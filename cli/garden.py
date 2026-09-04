#!/usr/bin/env python3
"""garden - repo-scoped, persistent agent stacks (ADR 0013).

One stack = one pod (opencode + litellm + squid) that can see exactly one
repository. Stacks run under rootless podman via `podman kube play`, locally
or on a remote host over ssh. Only the opencode port is ever published;
litellm and squid are pod-internal.

Stdlib + PyYAML only. All functions that render/derive are pure and tested
in tests/test_garden.py; anything that touches podman, ssh, or the vault is
isolated in cmd_* handlers.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets as secrets_mod
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

# --- Pins (drift-guarded against the Ansible roles by tests) ---------------------

IMAGE_LITELLM = "ghcr.io/berriai/litellm:v1.99.1"  # roles/litellm/defaults
IMAGE_SQUID = "localhost/squid:latest"  # roles/egress/files/squid.Containerfile
# Derived image (official + git, deploy/images/opencode.Containerfile); the
# official one has no git (verified 2026-09-04). Base pinned by tag+digest.
IMAGE_OPENCODE = "localhost/garden-opencode:1.18.27"

OPENCODE_CONTAINER_PORT = 4096
OPENCODE_PORT_BASE = 4096  # host ports allocated from here, first free
LITELLM_PORT = 4000  # pod-internal only, never published
SQUID_PORT = 3128  # pod-internal only, never published

PROXY_URL = f"http://127.0.0.1:{SQUID_PORT}"
LITELLM_BASE_URL = f"http://127.0.0.1:{LITELLM_PORT}/v1"

MEMORY_LIMITS = {"opencode": "512Mi", "litellm": "768Mi", "squid": "128Mi"}

# Mirrors roles/egress/defaults/main.yml (egress_allowlist_base) - drift test
# enforces equality. In-pod traffic is loopback or the pasta gateway only.
SQUID_ACL_SRC = ["127.0.0.0/8", "169.254.0.0/16"]
SQUID_ALLOWLIST_BASE = [
    "github.com",
    "api.github.com",
    ".githubusercontent.com",
    "codeload.github.com",
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "ghcr.io",
    "registry-1.docker.io",
    "auth.docker.io",
    "production.cloudflare.docker.com",
]
SQUID_MODEL_ENDPOINTS = ["api.fireworks.ai"]  # group_vars egress_model_endpoints

HARDENED = {
    "readOnlyRootFilesystem": True,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
}
# Squid starts as root and setuids to the proxy user (verified 2026-09-04:
# "initgroups: unable to set groups" crash with drop-ALL).
HARDENED_SQUID = {
    **HARDENED,
    "capabilities": {"drop": ["ALL"], "add": ["SETUID", "SETGID"]},
}

VAULT_MODEL = "secrets/model.yml"
VAULT_PASSWORD_FILE = "~/.config/infra/vault-password"


# --- Spec and state ------------------------------------------------------------


@dataclass
class StackSpec:
    name: str
    repo: str  # absolute path (mount) or clone URL (clone)
    mode: str = "mount"  # mount | clone
    port: int = OPENCODE_PORT_BASE
    host: str | None = None  # None = local; else ssh host alias (ADR 0014)
    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def pod(self) -> str:
        return f"garden-{self.name}"

    @property
    def secret_litellm(self) -> str:
        return f"{self.pod}-litellm"

    @property
    def secret_opencode(self) -> str:
        return f"{self.pod}-opencode"

    @property
    def volume_opencode(self) -> str:
        return f"{self.pod}-opencode"


def sanitize_name(raw: str) -> str:
    """DNS-1123-ish stack name from a repo dir or URL basename."""
    base = raw.rstrip("/").rsplit("/", 1)[-1]
    base = re.sub(r"\.git$", "", base)
    name = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    if not name or not name[0].isalnum():
        raise ValueError(f"cannot derive a stack name from {raw!r}; pass --name")
    return name[:40]


def state_dir() -> Path:
    return Path(
        os.environ.get("GARDEN_STATE_DIR", "~/.local/state/garden")
    ).expanduser()


class State:
    """~/.local/state/garden/state.json (0600, atomic writes)."""

    def __init__(self, root: Path | None = None):
        self.root = root or state_dir()
        self.file = self.root / "state.json"

    def load(self) -> dict:
        if not self.file.exists():
            return {"stacks": {}}
        return json.loads(self.file.read_text())

    def save(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.file)

    def add(self, spec: StackSpec, password: str = "") -> None:
        data = self.load()
        data["stacks"][spec.name] = {
            "repo": spec.repo,
            "mode": spec.mode,
            "port": spec.port,
            "host": spec.host,
            "created": spec.created,
            # opencode serve basic-auth password. Kept here (0600) rather than
            # re-derived from podman secrets - same protection level locally.
            "password": password,
        }
        self.save(data)

    def remove(self, name: str) -> bool:
        data = self.load()
        if data["stacks"].pop(name, None) is None:
            return False
        self.save(data)
        return True

    def get(self, name: str) -> dict | None:
        return self.load()["stacks"].get(name)

    def used_ports(self) -> set[int]:
        return {s["port"] for s in self.load()["stacks"].values()}


def allocate_port(used: set[int]) -> int:
    port = OPENCODE_PORT_BASE
    while port in used:
        port += 1
    if port > OPENCODE_PORT_BASE + 100:
        raise ValueError("no free stack ports (100 stacks is enough for anyone)")
    return port


# --- Renderers (pure) ----------------------------------------------------------


def render_allowlist() -> str:
    return "\n".join(SQUID_ALLOWLIST_BASE + SQUID_MODEL_ENDPOINTS) + "\n"


def render_squid_conf() -> str:
    src = " ".join(SQUID_ACL_SRC)
    return f"""# Rendered by garden (ADR 0013) - mirrors roles/egress/templates/squid.conf.j2.
http_port {SQUID_PORT}

acl allowed_src src {src}
acl allowed_sites dstdomain "/stack-config/allowlist.txt"
acl SSL_ports port 443
acl CONNECT method CONNECT

http_access deny CONNECT !SSL_ports
http_access deny !allowed_src
http_access allow allowed_sites
http_access deny all

# No caching: `cache deny all` suffices; Ubuntu's squid lacks the null store
# module, and the PID file must not persist across in-pod restarts (emptyDir
# is pod-scoped) or squid crash-loops on "already running". Both verified
# 2026-09-04.
cache deny all
pid_filename none

access_log stdio:/proc/self/fd/1
logfile_rotate 0
"""


def render_opencode_json(infra_repo: Path) -> str:
    """Stack variant of agent-config/opencode.json: in-pod litellm address and
    the master key via env (secretKeyRef) instead of an auth.json file."""
    src = json.loads((infra_repo / "agent-config/opencode.json").read_text())
    provider = src["provider"]["litellm"]
    provider["options"]["baseURL"] = LITELLM_BASE_URL
    provider["options"]["apiKey"] = "{env:LITELLM_MASTER_KEY}"
    return json.dumps(src, indent=2) + "\n"


def _secret_env(name: str, secret: str, key: str) -> dict:
    return {
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": secret, "key": key}},
    }


def render_pod(spec: StackSpec, infra_repo: Path) -> list[dict]:
    """The multi-document kube YAML (PVCs + Pod) for one stack."""
    publish_ip = "0.0.0.0" if spec.host else "127.0.0.1"
    stack_cfg = str(state_dir() / spec.name / "config")
    repo_path = spec.repo  # clone mode: CLI clones first, repo is the clone path

    opencode = {
        "name": "opencode",
        "image": IMAGE_OPENCODE,
        "args": ["serve", "--hostname", "0.0.0.0", "--port", str(OPENCODE_CONTAINER_PORT)],
        "workingDir": "/workspace",
        "env": [
            _secret_env("OPENCODE_SERVER_PASSWORD", spec.secret_opencode, "password"),
            _secret_env("LITELLM_MASTER_KEY", spec.secret_litellm, "master_key"),
            {"name": "HTTP_PROXY", "value": PROXY_URL},
            {"name": "HTTPS_PROXY", "value": PROXY_URL},
            # busybox/toybox tools only honor the lowercase forms
            {"name": "http_proxy", "value": PROXY_URL},
            {"name": "https_proxy", "value": PROXY_URL},
            {"name": "NO_PROXY", "value": "127.0.0.1,localhost"},
            {"name": "no_proxy", "value": "127.0.0.1,localhost"},
        ],
        "ports": [
            {
                "containerPort": OPENCODE_CONTAINER_PORT,
                "hostPort": spec.port,
                "hostIP": publish_ip,
            }
        ],
        "volumeMounts": [
            {"name": "repo", "mountPath": "/workspace"},
            # Directory mounts only - subPath file mounts bypass SELinux
            # relabeling and read as EACCES (verified 2026-09-04).
            {"name": "stack-config", "mountPath": "/root/.config/opencode",
             "readOnly": True},
            {"name": "opencode-home", "mountPath": "/root"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": MEMORY_LIMITS["opencode"]}},
        "securityContext": HARDENED,
        # kube play maps tcpSocket probes to `nc` inside the container, which
        # these minimal images lack (verified 2026-09-04) - exec probes with
        # tools each image actually ships: busybox nc here.
        "livenessProbe": {
            # 127.0.0.1, not "localhost": busybox nc tries only the first
            # resolved address (::1) and the server binds IPv4-only.
            "exec": {"command": ["sh", "-c", f"nc -z 127.0.0.1 {OPENCODE_CONTAINER_PORT} || exit 1"]},
            "initialDelaySeconds": 10,
            "periodSeconds": 30,
        },
    }

    litellm = {
        "name": "litellm",
        "image": IMAGE_LITELLM,
        "args": ["--config", "/agent-config/config.yaml", "--port", str(LITELLM_PORT), "--host", "0.0.0.0"],
        "env": [
            _secret_env("LITELLM_MASTER_KEY", spec.secret_litellm, "master_key"),
            _secret_env("LITELLM_SALT_KEY", spec.secret_litellm, "salt_key"),
            _secret_env("FIREWORKS_API_KEY", spec.secret_litellm, "fireworks_api_key"),
        ],
        "volumeMounts": [
            {"name": "agent-config", "mountPath": "/agent-config", "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": MEMORY_LIMITS["litellm"]}},
        "securityContext": HARDENED,
        "livenessProbe": {
            "exec": {"command": ["sh", "-c",
                                 f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {LITELLM_PORT}), 3)\" || exit 1"]},
            "initialDelaySeconds": 15,
            "periodSeconds": 30,
        },
    }

    squid = {
        "name": "squid",
        "image": IMAGE_SQUID,
        "args": ["-f", "/stack-config/squid.conf"],
        "volumeMounts": [
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
            {"name": "run", "mountPath": "/run"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": MEMORY_LIMITS["squid"]}},
        "securityContext": HARDENED_SQUID,
        "livenessProbe": {
            "exec": {"command": ["bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{SQUID_PORT} || exit 1"]},
            "initialDelaySeconds": 5,
            "periodSeconds": 30,
        },
    }

    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": spec.pod,
            "labels": {"app": "garden", "garden.io/stack": spec.name},
        },
        "spec": {
            "restartPolicy": "Always",
            "containers": [opencode, litellm, squid],
            "volumes": [
                {"name": "repo", "hostPath": {"path": repo_path, "type": "Directory"}},
                {
                    "name": "stack-config",
                    "hostPath": {"path": stack_cfg, "type": "Directory"},
                },
                {
                    "name": "agent-config",
                    "hostPath": {
                        "path": str(infra_repo / "agent-config/litellm"),
                        "type": "Directory",
                    },
                },
                {
                    "name": "opencode-home",
                    "persistentVolumeClaim": {"claimName": spec.volume_opencode},
                },
                {"name": "tmp", "emptyDir": {}},
                {"name": "run", "emptyDir": {}},
            ],
        },
    }

    def pvc(name: str) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
            },
        }

    return [pvc(spec.volume_opencode), pod]


def render_yaml(spec: StackSpec, infra_repo: Path) -> str:
    return yaml.safe_dump_all(render_pod(spec, infra_repo), sort_keys=True)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def render_secret_docs(spec: StackSpec, values: dict[str, str]) -> list[dict]:
    """K8s Secret docs for one stack. Only ever passed to kube play via stdin
    at up-time - never written to disk (ADR 0013)."""
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": spec.secret_litellm},
            "data": {
                "master_key": _b64(values["master_key"]),
                "salt_key": _b64(values["salt_key"]),
                "fireworks_api_key": _b64(values["fireworks_api_key"]),
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": spec.secret_opencode},
            "data": {"password": _b64(values["password"])},
        },
    ]


def container_names(spec: StackSpec) -> list[str]:
    """kube play prefixes container names with the pod name."""
    return [f"{spec.pod}-{c}" for c in ("opencode", "litellm", "squid")]


# --- Vault access (isolated; phase 11 uses this from `up`) ---------------------


def vault_key(key: str, vault_file: str = VAULT_MODEL) -> str:
    script = Path(__file__).parent.parent / "scripts/vault_get.py"
    return subprocess.run(
        [sys.executable, str(script), vault_file, key,
         "--password-file", VAULT_PASSWORD_FILE],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    ).stdout.strip()


# --- Runtime (podman, images, health) -------------------------------------------


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=input_text, text=True, check=check,
        capture_output=capture,
    )


def podman(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return run(["podman", *args], **kwargs)


def ensure_images(infra_repo: Path) -> None:
    """Squid and the opencode derivative are built (layer-cache makes this a
    no-op when unchanged); litellm is pulled once."""
    podman("build", "-q", "-t", IMAGE_SQUID, "-f",
           str(infra_repo / "ansible/roles/egress/files/squid.Containerfile"),
           str(infra_repo / "ansible/roles/egress/files"))
    podman("build", "-q", "-t", IMAGE_OPENCODE, "-f",
           str(infra_repo / "deploy/images/opencode.Containerfile"),
           str(infra_repo / "deploy/images"))
    if subprocess.run(["podman", "image", "exists", IMAGE_LITELLM]).returncode != 0:
        podman("pull", "-q", IMAGE_LITELLM)


def wait_healthy(spec: StackSpec, timeout: int = 240) -> None:
    """kube play maps livenessProbe to a podman healthcheck; wait on those."""
    deadline = time.monotonic() + timeout
    pending = set(container_names(spec))
    while time.monotonic() < deadline:
        for name in sorted(pending):
            status = podman(
                "inspect", "--format",
                "{{.State.Status}}:{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                name, capture=True,
            ).stdout.strip()
            if status.startswith("exited") or status.startswith("dead"):
                logs = podman("logs", "--tail", "20", name,
                              capture=True, check=False)
                raise RuntimeError(
                    f"container {name} died ({status}):\n{logs.stdout}{logs.stderr}"
                )
            if status.endswith("healthy"):
                pending.discard(name)
        if not pending:
            return
        time.sleep(3)
    raise RuntimeError(f"timed out waiting for healthy: {sorted(pending)}")


def label_for_containers(path: Path) -> None:
    """Shared SELinux label for hostPath sources. podman kube play does NOT
    reliably relabel hostPath volumes (observed: user_tmp_t/gconf_home_t left
    as-is; once even private :Z MCS categories that then blocked other pods).
    chcon -R is the explicit equivalent of a shared :z. The `-l s0` matters:
    a previous kube play left *private* MCS categories (cX,cY) on these files,
    which then blocked every other pod. Harmless for the owning user
    (unconfined_t can still read/write)."""
    run(["chcon", "-R", "-t", "container_file_t", "-l", "s0", str(path)])


def write_stack_config(root: Path, spec: StackSpec, infra_repo: Path) -> None:
    cfg = root / spec.name / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(root / spec.name, 0o700)
    os.chmod(cfg, 0o700)
    for filename, content in {
        "squid.conf": render_squid_conf(),
        "allowlist.txt": render_allowlist(),
        "opencode.json": render_opencode_json(infra_repo),
    }.items():
        path = cfg / filename
        path.write_text(content)
        os.chmod(path, 0o600)
    label_for_containers(root / spec.name)


def safe_rmtree(root: Path, path: Path) -> None:
    """Refuse to delete anything outside the garden state dir (a mounted repo
    must never be touched by `down --purge`)."""
    resolved = path.resolve()
    if not str(resolved).startswith(str(root.resolve()) + os.sep):
        raise ValueError(f"refusing to remove {resolved} - outside {root}")
    shutil.rmtree(resolved, ignore_errors=True)


def stack_config_dir(root: Path, spec: StackSpec) -> Path:
    return root / spec.name / "config"


def quadlet_path(spec: StackSpec) -> Path:
    return Path(
        os.environ.get(
            "GARDEN_QUADLET_DIR",
            "~/.config/containers/systemd",
        )
    ).expanduser() / f"{spec.pod}.kube"


def render_quadlet(spec: StackSpec, pod_yaml: Path) -> str:
    """Boot/crash persistence: systemd plays the pod-only YAML (secrets live
    in the podman store, created once at up time and referenced by name)."""
    return f"""# Generated by garden (ADR 0013). Removed by `garden down`.
[Unit]
Description=garden stack {spec.name} (repo-scoped agent pod)

[Kube]
Yaml={pod_yaml}

[Install]
WantedBy=default.target
"""


def linger_enabled() -> bool:
    out = subprocess.run(
        ["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger",
         "--value"],
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip() == "yes"


WATCHDOG_SERVICE = """# Generated by garden (ADR 0013). Restarts crashed stack containers.
# Under a systemd unit, podman delegates container restart policy to systemd
# (event log: died -> cleanup, no restart), so a killed stack container would
# otherwise stay dead until the next boot. Verified 2026-09-04.
[Unit]
Description=garden watchdog: restart exited stack containers

[Service]
Type=oneshot
ExecStart=/bin/sh -c "podman ps -aq --filter label=app=garden --filter status=exited | xargs -r podman container restart"
"""

WATCHDOG_TIMER = """# Generated by garden (ADR 0013).
[Unit]
Description=garden watchdog timer

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s

[Install]
WantedBy=timers.target
"""


def ensure_watchdog() -> None:
    """Shared per-user watchdog (idempotent)."""
    unit_dir = Path("~/.config/systemd/user").expanduser()
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (("garden-watchdog.service", WATCHDOG_SERVICE),
                       ("garden-watchdog.timer", WATCHDOG_TIMER)):
        path = unit_dir / name
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", "-q", "garden-watchdog.timer"])


# --- Commands ------------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    infra_repo = Path(__file__).parent.parent.resolve()
    name = args.name or sanitize_name(args.repo)
    spec = StackSpec(
        name=name,
        repo=str(Path(args.repo).expanduser().resolve()),
        mode="clone" if args.clone else "mount",
        port=args.port or allocate_port(State().used_ports()),
        host=args.host,
    )
    sys.stdout.write(render_yaml(spec, infra_repo))
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    if args.host:
        raise ValueError("remote mode arrives in phase 13 (ADR 0014)")
    infra_repo = Path(__file__).parent.parent.resolve()
    state = State()

    name = args.name or sanitize_name(args.repo)
    if args.clone:
        clone_dir = state.root / "clones" / name
        if not clone_dir.exists():
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", args.repo, str(clone_dir)])
        repo_path = clone_dir
        mode = "clone"
    else:
        repo_path = Path(args.repo).expanduser().resolve()
        if not repo_path.is_dir():
            raise ValueError(f"repo path does not exist: {repo_path}")
        mode = "mount"

    existing = state.get(name)
    port = existing["port"] if existing else allocate_port(state.used_ports())
    spec = StackSpec(name=name, repo=str(repo_path), mode=mode, port=port,
                     created=existing["created"] if existing else
                     datetime.now(timezone.utc).isoformat(timespec="seconds"))

    if sys.stdin.isatty() and not args.yes:
        print(f"stack:   {spec.pod}")
        print(f"repo:    {spec.repo} ({mode})")
        print(f"attach:  http://127.0.0.1:{port}")
        if input("Bring it up? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("aborted")
            return 1

    print("==> images")
    ensure_images(infra_repo)

    print("==> stack config")
    write_stack_config(state.root, spec, infra_repo)
    # Everything a container bind-mounts must carry container_file_t (shared).
    label_for_containers(infra_repo / "agent-config" / "litellm")
    label_for_containers(repo_path)

    values = {
        "master_key": secrets_mod.token_hex(32),
        "salt_key": secrets_mod.token_hex(32),
        "password": secrets_mod.token_hex(12),
        "fireworks_api_key": vault_key("fireworks_api_key"),
    }

    # Idempotent refresh: drop the old pod and secrets before replaying.
    podman("pod", "rm", "-f", spec.pod, check=False, capture=True)
    podman("secret", "rm", spec.secret_litellm, spec.secret_opencode,
           check=False, capture=True)

    print("==> secrets (stdin only, then they live in the podman store)")
    podman("kube", "play", "-",
           input_text=yaml.safe_dump_all(render_secret_docs(spec, values),
                                         sort_keys=True))

    # Pod-only YAML on disk for the quadlet; never contains secrets.
    pod_yaml = state.root / spec.name / "pod.yaml"
    pod_yaml.write_text(render_yaml(spec, infra_repo))
    os.chmod(pod_yaml, 0o600)

    if args.no_install:
        print("==> kube play (no boot persistence)")
        podman("kube", "play", "--replace", str(pod_yaml))
    else:
        qpath = quadlet_path(spec)
        qpath.parent.mkdir(parents=True, exist_ok=True)
        qpath.write_text(render_quadlet(spec, pod_yaml))
        run(["systemctl", "--user", "daemon-reload"])
        print(f"==> systemd start ({qpath.name})")
        run(["systemctl", "--user", "restart", f"{spec.pod}.service"])
        ensure_watchdog()
        if not linger_enabled():
            print("!! linger is off: stacks start at login, not at boot.")
            print(f"   enable once with: sudo loginctl enable-linger {os.environ.get('USER')}")

    print("==> waiting for healthy")
    wait_healthy(spec)
    state.add(spec, password=values["password"])

    url = f"http://127.0.0.1:{port}"
    print(f"\nstack up: {url}  (user: opencode, password: {values['password']})")
    if not args.no_attach and sys.stdin.isatty() and shutil.which("opencode"):
        os.execvp("opencode", ["opencode", "attach", url,
                               "--username", "opencode",
                               "--password", values["password"]])
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    state = State()
    record = state.get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (garden ls)")
    spec = StackSpec(name=args.name, repo=record["repo"], mode=record["mode"],
                     port=record["port"], host=record["host"])
    qpath = quadlet_path(spec)
    if qpath.exists():
        qpath.unlink()
        run(["systemctl", "--user", "daemon-reload"], check=False)
    podman("pod", "stop", "-t", "5", spec.pod, check=False, capture=True)
    podman("pod", "rm", "-f", spec.pod, check=False, capture=True)
    if args.purge:
        podman("volume", "rm", "-f", spec.volume_opencode,
               check=False, capture=True)
        podman("secret", "rm", spec.secret_litellm, spec.secret_opencode,
               check=False, capture=True)
        safe_rmtree(state.root, state.root / args.name / "config")
        safe_rmtree(state.root, state.root / args.name)
        if record["mode"] == "clone":
            safe_rmtree(state.root, state.root / "clones" / args.name)
        state.remove(args.name)
        print(f"purged {args.name}")
    else:
        print(f"stopped {args.name} (volumes, secrets and state kept; "
              f"`garden up --repo {record['repo']} --name {args.name}` restarts it, "
              f"`garden down {args.name} --purge` deletes everything)")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (garden ls)")
    url = f"http://127.0.0.1:{record['port']}"
    if not shutil.which("opencode"):
        print(f"opencode CLI not found; attach manually: {url} "
              f"(user: opencode, password: {record['password']})")
        return 1
    os.execvp("opencode", ["opencode", "attach", url,
                           "--username", "opencode",
                           "--password", record["password"]])
    return 0  # unreachable


def cmd_logs(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (garden ls)")
    if args.container:
        cmd = ["podman", "logs"] + (["-f"] if args.follow else []) + \
              [f"garden-{args.name}-{args.container}"]
    else:
        cmd = ["podman", "pod", "logs"] + (["-f"] if args.follow else []) + \
              [f"garden-{args.name}"]
    os.execvp("podman", cmd)
    return 0  # unreachable


def cmd_ls(args: argparse.Namespace) -> int:
    stacks = State().load()["stacks"]
    if not stacks:
        print("no stacks - run `garden up` inside a repo")
        return 0
    live = {}
    result = podman("pod", "ps", "--format", "json",
                    "--filter", "name=^garden-", capture=True, check=False)
    if result.returncode == 0 and result.stdout.strip():
        for pod in json.loads(result.stdout):
            live[pod["Name"]] = pod["Status"]
    print(f"{'NAME':<20} {'STATUS':<12} {'PERSIST':<8} {'HOST':<7} {'PORT':<6} REPO")
    for name, s in sorted(stacks.items()):
        status = live.get(f"garden-{name}", "down")
        spec = StackSpec(name=name, repo=s["repo"])
        persist = "quadlet" if quadlet_path(spec).exists() else "-"
        print(f"{name:<20} {status:<12} {persist:<8} {s['host'] or 'local':<7} "
              f"{s['port']:<6} {s['repo']} ({s['mode']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="garden", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_render = sub.add_parser("render", help="print the kube YAML for a stack")
    p_render.add_argument("--repo", default=os.environ.get("GARDEN_REPO", "."))
    p_render.add_argument("--name", default=os.environ.get("GARDEN_NAME"))
    p_render.add_argument("--port", type=int, default=None)
    p_render.add_argument("--host", default=os.environ.get("GARDEN_HOST"))
    p_render.add_argument("--clone", action="store_true",
                          help="repo is a URL; clone instead of mounting")
    p_render.set_defaults(func=cmd_render)

    p_ls = sub.add_parser("ls", help="list stacks")
    p_ls.set_defaults(func=cmd_ls)

    p_up = sub.add_parser("up", help="bring a stack up (default repo: cwd)")
    p_up.add_argument("--repo", default=os.environ.get("GARDEN_REPO", "."))
    p_up.add_argument("--name", default=os.environ.get("GARDEN_NAME"))
    p_up.add_argument("--host", default=os.environ.get("GARDEN_HOST"))
    p_up.add_argument("--clone", action="store_true",
                      default=os.environ.get("GARDEN_CLONE") == "1",
                      help="repo is a URL; clone into the state dir")
    p_up.add_argument("--no-attach", action="store_true",
                      default=os.environ.get("GARDEN_NO_ATTACH") == "1")
    p_up.add_argument("--no-install", action="store_true",
                      default=os.environ.get("GARDEN_NO_INSTALL") == "1",
                      help="no systemd quadlet: dies at reboot")
    p_up.add_argument("-y", "--yes", action="store_true",
                      default=os.environ.get("GARDEN_YES") == "1")
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="stop a stack (keeps volumes/state)")
    p_down.add_argument("name")
    p_down.add_argument("--purge", action="store_true",
                        help="also delete volumes, secrets, config and state")
    p_down.set_defaults(func=cmd_down)

    p_attach = sub.add_parser("attach", help="attach the opencode TUI to a stack")
    p_attach.add_argument("name")
    p_attach.set_defaults(func=cmd_attach)

    p_logs = sub.add_parser("logs", help="pod logs (or one container)")
    p_logs.add_argument("name")
    p_logs.add_argument("container", nargs="?",
                        choices=["opencode", "litellm", "squid"])
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.set_defaults(func=cmd_logs)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"garden: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
