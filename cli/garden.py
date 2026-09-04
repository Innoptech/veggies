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
VAULT_GITHUB = "secrets/github.yml"
VAULT_PASSWORD_FILE = "~/.config/infra/vault-password"

# Remote mode (ADR 0014): everything runs as this user over ssh+sudo.
REMOTE_USER = "stacks"
REMOTE_STATE_ROOT = f"/home/{REMOTE_USER}/.local/state/garden"


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

    @property
    def is_remote(self) -> bool:
        return self.host is not None

    def state_root(self) -> str:
        """Where this stack's files live on ITS host (rendered into YAML)."""
        return REMOTE_STATE_ROOT if self.is_remote else str(state_dir())

    def config_dir(self) -> str:
        return f"{self.state_root()}/{self.name}/config"

    def pod_yaml_path(self) -> str:
        return f"{self.state_root()}/{self.name}/pod.yaml"


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
    """The multi-document kube YAML (PVCs + Pod) for one stack. All hostPath
    values are paths on the host the stack runs on (ADR 0014)."""
    publish_ip = "0.0.0.0" if spec.host else "127.0.0.1"
    stack_cfg = spec.config_dir()
    repo_path = spec.repo  # clone mode: CLI clones first, repo is the clone path
    # Local: live-mount the vendored config (edit + `garden up` to apply).
    # Remote: a rendered copy sits in the stack's config dir on that host.
    litellm_cfg = (
        spec.config_dir() if spec.is_remote
        else str(infra_repo / "agent-config" / "litellm")
    )

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
                    "hostPath": {"path": litellm_cfg, "type": "Directory"},
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


def host_run(host: str | None, args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command on the stack's host. Remote = ssh + passwordless sudo
    into the stacks user (ADR 0014); stdin (kube YAML, secrets) pipes through."""
    if host is None:
        return run(args, **kwargs)
    return run(["ssh", host, "sudo", "-n", "-iu", REMOTE_USER, *args], **kwargs)


def host_podman(host: str | None, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return host_run(host, ["podman", *args], **kwargs)


def host_systemctl(host: str | None, *args: str, **kwargs) -> subprocess.CompletedProcess:
    if host is None:
        return run(["systemctl", "--user", *args], **kwargs)
    return host_run(host, ["systemctl", "--user", *args], **kwargs)


def host_write(host: str | None, path: str, content: str, mode: int = 0o600) -> None:
    if host is None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        os.chmod(p, mode)
        return
    import shlex
    q = shlex.quote(path)
    host_run(host, ["sh", "-c",
                    f"umask 077 && mkdir -p $(dirname {q}) && cat > {q}"],
             input_text=content)


def quadlet_dir(host: str | None) -> str:
    if host is None:
        return str(Path(os.environ.get(
            "GARDEN_QUADLET_DIR", "~/.config/containers/systemd")).expanduser())
    return f"/home/{REMOTE_USER}/.config/containers/systemd"


def host_exists(host: str | None, path: str, kind: str = "f") -> bool:
    result = host_run(host, ["test", f"-{kind}", path], check=False,
                      capture=True)
    return result.returncode == 0


def ensure_images(host: str | None, infra_repo: Path) -> None:
    """Squid and the opencode derivative are built (layer-cache makes this a
    no-op when unchanged); litellm is pulled once. Remote: the Containerfiles
    are shipped into the remote state dir and built there (ADR 0014)."""
    squid_cf = (infra_repo / "ansible/roles/egress/files/squid.Containerfile").read_text()
    opencode_cf = (infra_repo / "deploy/images/opencode.Containerfile").read_text()
    if host is None:
        (infra_repo / "deploy/images").mkdir(parents=True, exist_ok=True)
        host_build_dir = None
    images_dir = f"{REMOTE_STATE_ROOT}/images" if host else None
    for image, containerfile in ((IMAGE_SQUID, squid_cf), (IMAGE_OPENCODE, opencode_cf)):
        if host is None:
            cf_path = str(state_dir() / "images" / f"{image.split('/')[-1].split(':')[0]}.Containerfile")
            host_write(None, cf_path, containerfile)
            host_podman(None, "build", "-q", "-t", image, "-f", cf_path,
                        str(state_dir() / "images"))
        else:
            cf_path = f"{images_dir}/{image.split('/')[-1].split(':')[0]}.Containerfile"
            host_write(host, cf_path, containerfile)
            host_podman(host, "build", "-q", "-t", image, "-f", cf_path, images_dir)
    if host_podman(host, "image", "exists", IMAGE_LITELLM,
                   check=False, capture=True).returncode != 0:
        host_podman(host, "pull", "-q", IMAGE_LITELLM)


def wait_healthy(spec: StackSpec, timeout: int = 240) -> None:
    """kube play maps livenessProbe to a podman healthcheck; wait on those."""
    deadline = time.monotonic() + timeout
    pending = set(container_names(spec))
    while time.monotonic() < deadline:
        for name in sorted(pending):
            status = host_podman(
                spec.host,
                "inspect", "--format",
                "{{.State.Status}}:{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                name, capture=True,
            ).stdout.strip()
            if status.startswith("exited") or status.startswith("dead"):
                logs = host_podman(spec.host, "logs", "--tail", "20", name,
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


def label_for_containers(host: str | None, path: str) -> None:
    """Shared SELinux label for hostPath sources. podman kube play does NOT
    reliably relabel hostPath volumes (observed: user_tmp_t/gconf_home_t left
    as-is; once even private :Z MCS categories that then blocked other pods).
    chcon -R is the explicit equivalent of a shared :z. The `-l s0` matters:
    a previous kube play left *private* MCS categories (cX,cY) on these files,
    which then blocked every other pod. Harmless for the owning user
    (unconfined_t can still read/write)."""
    host_run(host, ["chcon", "-R", "-t", "container_file_t", "-l", "s0", path])


def write_stack_config(spec: StackSpec, infra_repo: Path) -> None:
    files = {
        "squid.conf": render_squid_conf(),
        "allowlist.txt": render_allowlist(),
        "opencode.json": render_opencode_json(infra_repo),
    }
    if spec.is_remote:
        # No infra checkout on the VPS: ship the litellm config as a copy.
        files["config.yaml"] = (
            infra_repo / "agent-config/litellm/config.yaml").read_text()
    for filename, content in files.items():
        host_write(spec.host, f"{spec.config_dir()}/{filename}", content)
    label_for_containers(spec.host, f"{spec.state_root()}/{spec.name}")


def safe_rmtree(host: str | None, root: str, path: str) -> None:
    """Refuse to delete anything outside the garden state dir (a mounted repo
    must never be touched by `down --purge`)."""
    if not (path == root or path.startswith(root.rstrip("/") + "/")):
        raise ValueError(f"refusing to remove {path} - outside {root}")
    if host is None:
        shutil.rmtree(path, ignore_errors=True)
    else:
        host_run(host, ["rm", "-rf", path])


def quadlet_path(spec: StackSpec) -> str:
    return f"{quadlet_dir(spec.host)}/{spec.pod}.kube"


def render_quadlet(spec: StackSpec, pod_yaml: str) -> str:
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


def ensure_watchdog(host: str | None) -> None:
    """Shared per-user watchdog (idempotent)."""
    if host is None:
        unit_dir = str(Path("~/.config/systemd/user").expanduser())
    else:
        unit_dir = f"/home/{REMOTE_USER}/.config/systemd/user"
    for name, text in (("garden-watchdog.service", WATCHDOG_SERVICE),
                       ("garden-watchdog.timer", WATCHDOG_TIMER)):
        host_write(host, f"{unit_dir}/{name}", text, mode=0o644)
    host_systemctl(host, "daemon-reload")
    host_systemctl(host, "enable", "--now", "-q", "garden-watchdog.timer")


# --- Commands ------------------------------------------------------------------


def stack_url(record: dict) -> str:
    """Attach URL: loopback locally, the tailnet name for remote stacks."""
    host = record["host"] or "127.0.0.1"
    return f"http://{host}:{record['port']}"


def cmd_render(args: argparse.Namespace) -> int:
    infra_repo = Path(__file__).parent.parent.resolve()
    name = args.name or sanitize_name(args.repo)
    if args.clone and args.host:
        repo = f"{REMOTE_STATE_ROOT}/clones/{name}"  # matches cmd_up
    elif args.clone:
        repo = str(state_dir() / "clones" / name)
    else:
        repo = str(Path(args.repo).expanduser().resolve())
    spec = StackSpec(
        name=name,
        repo=repo,
        mode="clone" if args.clone else "mount",
        port=args.port or allocate_port(State().used_ports()),
        host=args.host,
    )
    sys.stdout.write(render_yaml(spec, infra_repo))
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    infra_repo = Path(__file__).parent.parent.resolve()
    state = State()
    host = args.host

    name = args.name or sanitize_name(args.repo)
    if host and not args.clone:
        raise ValueError(
            "remote stacks are clone-mode: pass --clone with a git URL "
            "(a local bind-mount is meaningless on another host)"
        )
    if args.clone:
        if host:
            clone_dir = f"{REMOTE_STATE_ROOT}/clones/{name}"
            if not host_exists(host, clone_dir, kind="d"):
                clone_cmd = ["git", "clone"]
                if args.repo.startswith("https://github.com/"):
                    # Token rides the process list on the VPS briefly - see
                    # the threat model (ADR 0014). ssh URLs need no token.
                    token = vault_key("github_token", VAULT_GITHUB)
                    clone_cmd += ["-c",
                                  f"http.extraHeader=Authorization: Bearer {token}"]
                clone_cmd += [args.repo, clone_dir]
                host_run(host, clone_cmd)
        else:
            clone_path = state.root / "clones" / name
            if not clone_path.exists():
                clone_path.parent.mkdir(parents=True, exist_ok=True)
                run(["git", "clone", args.repo, str(clone_path)])
            clone_dir = str(clone_path)
        repo_path = clone_dir
        mode = "clone"
    else:
        repo_path = str(Path(args.repo).expanduser().resolve())
        if not Path(repo_path).is_dir():
            raise ValueError(f"repo path does not exist: {repo_path}")
        mode = "mount"

    existing = state.get(name)
    port = existing["port"] if existing else allocate_port(state.used_ports())
    spec = StackSpec(name=name, repo=repo_path, mode=mode, port=port, host=host,
                     created=existing["created"] if existing else
                     datetime.now(timezone.utc).isoformat(timespec="seconds"))

    url = f"http://{host or '127.0.0.1'}:{port}"
    if sys.stdin.isatty() and not args.yes:
        print(f"stack:   {spec.pod} on {host or 'this machine'}")
        print(f"repo:    {spec.repo} ({mode})")
        print(f"attach:  {url}")
        if input("Bring it up? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("aborted")
            return 1

    print("==> images")
    ensure_images(host, infra_repo)

    print("==> stack config")
    write_stack_config(spec, infra_repo)
    if host is None:
        # Everything a container bind-mounts must carry container_file_t.
        label_for_containers(None, str(infra_repo / "agent-config" / "litellm"))
        label_for_containers(None, repo_path)

    values = {
        "master_key": secrets_mod.token_hex(32),
        "salt_key": secrets_mod.token_hex(32),
        "password": secrets_mod.token_hex(12),
        "fireworks_api_key": vault_key("fireworks_api_key"),
    }

    # Idempotent refresh: drop the old pod and secrets before replaying.
    host_podman(host, "pod", "rm", "-f", spec.pod, check=False, capture=True)
    host_podman(host, "secret", "rm", spec.secret_litellm, spec.secret_opencode,
                check=False, capture=True)

    print("==> secrets (stdin only, then they live in the podman store)")
    host_podman(host, "kube", "play", "-",
                input_text=yaml.safe_dump_all(render_secret_docs(spec, values),
                                              sort_keys=True))

    # Pod-only YAML on disk for the quadlet; never contains secrets.
    host_write(host, spec.pod_yaml_path(), render_yaml(spec, infra_repo))

    if args.no_install:
        print("==> kube play (no boot persistence)")
        host_podman(host, "kube", "play", "--replace", spec.pod_yaml_path())
    else:
        host_write(host, quadlet_path(spec),
                   render_quadlet(spec, spec.pod_yaml_path()), mode=0o644)
        host_systemctl(host, "daemon-reload")
        print(f"==> systemd start ({spec.pod}.service)")
        host_systemctl(host, "restart", f"{spec.pod}.service")
        ensure_watchdog(host)
        if host is None and not linger_enabled():
            print("!! linger is off: stacks start at login, not at boot.")
            print(f"   enable once with: sudo loginctl enable-linger {os.environ.get('USER')}")

    print("==> waiting for healthy")
    wait_healthy(spec)
    state.add(spec, password=values["password"])

    print(f"\nstack up: {url}  (user: opencode, password: {values['password']})")
    if host:
        print("remote attach needs your tailnet up (ADR 0003/0014)")
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
    host = spec.host
    if host_exists(host, quadlet_path(spec)):
        host_run(host, ["rm", "-f", quadlet_path(spec)])
        host_systemctl(host, "daemon-reload", check=False)
    host_podman(host, "pod", "stop", "-t", "5", spec.pod, check=False, capture=True)
    host_podman(host, "pod", "rm", "-f", spec.pod, check=False, capture=True)
    if args.purge:
        host_podman(host, "volume", "rm", "-f", spec.volume_opencode,
                    check=False, capture=True)
        host_podman(host, "secret", "rm", spec.secret_litellm, spec.secret_opencode,
                    check=False, capture=True)
        safe_rmtree(host, spec.state_root(), f"{spec.state_root()}/{args.name}")
        if record["mode"] == "clone":
            safe_rmtree(host, spec.state_root(),
                        f"{spec.state_root()}/clones/{args.name}")
        state.remove(args.name)
        print(f"purged {args.name}")
    else:
        print(f"stopped {args.name} (volumes, secrets and state kept; "
              f"`garden down {args.name} --purge` deletes everything)")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (garden ls)")
    url = stack_url(record)
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
    if record["host"]:
        os.execvp("ssh", ["ssh", record["host"], "sudo", "-n", "-iu",
                          REMOTE_USER, *cmd])
    os.execvp("podman", cmd)
    return 0  # unreachable


def cmd_ls(args: argparse.Namespace) -> int:
    stacks = State().load()["stacks"]
    if not stacks:
        print("no stacks - run `garden up` inside a repo")
        return 0
    live: dict[str, str] = {}
    hosts = {None} | {s["host"] for s in stacks.values() if s["host"]}
    for host in hosts:
        result = host_podman(host, "pod", "ps", "--format", "json",
                             "--filter", "name=^garden-",
                             capture=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            for pod in json.loads(result.stdout):
                live[pod["Name"]] = pod["Status"]
    print(f"{'NAME':<20} {'STATUS':<12} {'PERSIST':<8} {'HOST':<7} {'PORT':<6} REPO")
    for name, s in sorted(stacks.items()):
        status = live.get(f"garden-{name}", "down")
        spec = StackSpec(name=name, repo=s["repo"], host=s["host"])
        persist = "quadlet" if host_exists(spec.host, quadlet_path(spec)) else "-"
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
