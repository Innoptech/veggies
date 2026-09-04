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
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

# --- Pins (drift-guarded against the Ansible roles by tests) ---------------------

IMAGE_LITELLM = "ghcr.io/berriai/litellm:v1.99.1"  # roles/litellm/defaults
IMAGE_SQUID = "localhost/squid:latest"  # roles/egress/files/squid.Containerfile
# TODO(verify): official image exists per opencode docs; confirm the tag scheme
# at first pull. Fallback: build from the pinned tarball like the opencode role.
IMAGE_OPENCODE = "ghcr.io/anomalyco/opencode:v1.18.27"

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

    @property
    def volume_litellm(self) -> str:
        return f"{self.pod}-litellm"


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

    def add(self, spec: StackSpec) -> None:
        data = self.load()
        data["stacks"][spec.name] = {
            "repo": spec.repo,
            "mode": spec.mode,
            "port": spec.port,
            "host": spec.host,
            "created": spec.created,
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
acl allowed_sites dstdomain "/etc/squid/allowlist.txt"
acl SSL_ports port 443
acl CONNECT method CONNECT

http_access deny CONNECT !SSL_ports
http_access deny !allowed_src
http_access allow allowed_sites
http_access deny all

cache deny all
cache_dir null /var/spool/squid-null

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
            {"name": "NO_PROXY", "value": "127.0.0.1,localhost"},
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
            {"name": "stack-config", "mountPath": "/config", "readOnly": True},
            {"name": "opencode-home", "mountPath": "/root"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": MEMORY_LIMITS["opencode"]}},
        "securityContext": HARDENED,
        "livenessProbe": {
            "tcpSocket": {"port": OPENCODE_CONTAINER_PORT},
            "initialDelaySeconds": 10,
            "periodSeconds": 30,
        },
    }

    litellm = {
        "name": "litellm",
        "image": IMAGE_LITELLM,
        "args": ["--config", "/app/config.yaml", "--port", str(LITELLM_PORT), "--host", "0.0.0.0"],
        "env": [
            _secret_env("LITELLM_MASTER_KEY", spec.secret_litellm, "master_key"),
            _secret_env("LITELLM_SALT_KEY", spec.secret_litellm, "salt_key"),
            _secret_env("FIREWORKS_API_KEY", spec.secret_litellm, "fireworks_api_key"),
            {"name": "DATABASE_URL", "value": "sqlite:////data/litellm.db"},
        ],
        "volumeMounts": [
            {
                "name": "litellm-config",
                "mountPath": "/app/config.yaml",
                "subPath": "config.yaml",
                "readOnly": True,
            },
            {"name": "litellm-data", "mountPath": "/data"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": MEMORY_LIMITS["litellm"]}},
        "securityContext": HARDENED,
        # TODO(verify): endpoint path at first `garden up` smoke.
        "livenessProbe": {
            "httpGet": {"path": "/health/liveliness", "port": LITELLM_PORT},
            "initialDelaySeconds": 15,
            "periodSeconds": 30,
        },
    }

    squid = {
        "name": "squid",
        "image": IMAGE_SQUID,
        "volumeMounts": [
            {
                "name": "stack-config",
                "mountPath": "/etc/squid/squid.conf",
                "subPath": "squid.conf",
                "readOnly": True,
            },
            {
                "name": "stack-config",
                "mountPath": "/etc/squid/allowlist.txt",
                "subPath": "allowlist.txt",
                "readOnly": True,
            },
            {"name": "run", "mountPath": "/run"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": MEMORY_LIMITS["squid"]}},
        "securityContext": HARDENED,
        "livenessProbe": {
            "tcpSocket": {"port": SQUID_PORT},
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
                    "name": "litellm-config",
                    "hostPath": {
                        "path": str(infra_repo / "agent-config/litellm"),
                        "type": "Directory",
                    },
                },
                {
                    "name": "opencode-home",
                    "persistentVolumeClaim": {"claimName": spec.volume_opencode},
                },
                {
                    "name": "litellm-data",
                    "persistentVolumeClaim": {"claimName": spec.volume_litellm},
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

    return [pvc(spec.volume_opencode), pvc(spec.volume_litellm), pod]


def render_yaml(spec: StackSpec, infra_repo: Path) -> str:
    return yaml.safe_dump_all(render_pod(spec, infra_repo), sort_keys=True)


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


def cmd_ls(args: argparse.Namespace) -> int:
    stacks = State().load()["stacks"]
    if not stacks:
        print("no stacks - run `garden up` inside a repo")
        return 0
    print(f"{'NAME':<20} {'HOST':<10} {'PORT':<6} {'MODE':<6} REPO")
    for name, s in sorted(stacks.items()):
        print(f"{name:<20} {s['host'] or 'local':<10} {s['port']:<6} {s['mode']:<6} {s['repo']}")
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

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"garden: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
