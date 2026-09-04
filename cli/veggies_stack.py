"""veggies stack definition - the component model (ADR 0016).

A stack is a list of Components. Each component renders its container spec
and declares its pod volumes; render_pod composes them. `CORE` is the
default stack (opencode + litellm + squid, ADR 0013); future MCPs, agent
workers, and the orchestrator (ADRs 0017-0019) are new entries, not new
monolith.

Pure renderers only: nothing in this module touches podman, ssh, the
network, or the vault. Everything is pytest-covered via tests/test_veggies.py
(which imports these names re-exported through cli/veggies.py).
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

# --- Pins (drift-guarded against the Ansible roles by tests) ---------------------

# Sole owner of this pin since ADR 0016 (roles/litellm deleted).
IMAGE_LITELLM = "ghcr.io/berriai/litellm:v1.99.1"
IMAGE_SQUID = "localhost/squid:latest"  # ansible/roles/egress/files/squid.Containerfile
# Derived image (official + git, deploy/images/opencode.Containerfile); the
# official one has no git (verified 2026-09-04). Base pinned by tag+digest.
IMAGE_OPENCODE = "localhost/veggies-opencode:1.18.27"

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

# Remote mode (ADR 0014): everything runs as this user over ssh+sudo.
REMOTE_USER = "stacks"
REMOTE_STATE_ROOT = f"/home/{REMOTE_USER}/.local/state/veggies"


# --- Spec ----------------------------------------------------------------------


@dataclass
class StackSpec:
    name: str
    repo: str  # absolute path (mount) or clone URL (clone)
    mode: str = "mount"  # mount | clone
    port: int = OPENCODE_PORT_BASE
    host: str | None = None  # None = local; else ssh host alias (ADR 0014)
    model: str | None = None  # litellm alias, from veggies.yml or --model
    components: list[str] | None = None  # component names; None = CORE
    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def pod(self) -> str:
        return f"veggies-{self.name}"

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
        os.environ.get("VEGGIES_STATE_DIR", "~/.local/state/veggies")
    ).expanduser()


def allocate_port(used: set[int]) -> int:
    port = OPENCODE_PORT_BASE
    while port in used:
        port += 1
    if port > OPENCODE_PORT_BASE + 100:
        raise ValueError("no free stack ports (100 stacks is enough for anyone)")
    return port


# --- Config renderers (pure) -----------------------------------------------------


def render_allowlist() -> str:
    return "\n".join(SQUID_ALLOWLIST_BASE + SQUID_MODEL_ENDPOINTS) + "\n"


def render_squid_conf() -> str:
    src = " ".join(SQUID_ACL_SRC)
    return f"""# Rendered by veggies (ADR 0013) - mirrors roles/egress/templates/squid.conf.j2.
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


def render_opencode_json(infra_repo: Path, model: str | None = None) -> str:
    """Stack variant of agent-config/opencode.json: in-pod litellm address and
    the master key via env (secretKeyRef) instead of an auth.json file."""
    src = json.loads((infra_repo / "agent-config/opencode.json").read_text())
    provider = src["provider"]["litellm"]
    provider["options"]["baseURL"] = LITELLM_BASE_URL
    provider["options"]["apiKey"] = "{env:LITELLM_MASTER_KEY}"
    if model:
        src["model"] = f"litellm/{model}"
    return json.dumps(src, indent=2) + "\n"


# --- Components ------------------------------------------------------------------


def _secret_env(name: str, secret: str, key: str) -> dict:
    return {
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": secret, "key": key}},
    }


@dataclass(frozen=True)
class Component:
    """One member of a stack pod: renders a container and declares its volumes.

    volumes() may declare a volume another component also mounts (e.g.
    stack-config); first declaration wins, duplicates are merged by name."""

    name: str
    render: Callable[[StackSpec, Path], dict]
    volumes: Callable[[StackSpec, Path], list[dict]]


def _opencode_container(spec: StackSpec, infra_repo: Path) -> dict:
    publish_ip = "0.0.0.0" if spec.host else "127.0.0.1"
    return {
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


def _opencode_volumes(spec: StackSpec, infra_repo: Path) -> list[dict]:
    repo_path = spec.repo  # clone mode: CLI clones first, repo is the clone path
    return [
        {"name": "repo", "hostPath": {"path": repo_path, "type": "Directory"}},
        {
            "name": "opencode-home",
            "persistentVolumeClaim": {"claimName": spec.volume_opencode},
        },
        {"name": "tmp", "emptyDir": {}},
    ]


def _litellm_container(spec: StackSpec, infra_repo: Path) -> dict:
    return {
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


def _litellm_volumes(spec: StackSpec, infra_repo: Path) -> list[dict]:
    # Local: live-mount the vendored config (edit + `veggies up` to apply).
    # Remote: a rendered copy sits in the stack's config dir on that host.
    litellm_cfg = (
        spec.config_dir() if spec.is_remote
        else str(infra_repo / "agent-config" / "litellm")
    )
    return [
        {"name": "agent-config", "hostPath": {"path": litellm_cfg, "type": "Directory"}},
    ]


def _squid_container(spec: StackSpec, infra_repo: Path) -> dict:
    return {
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


def _squid_volumes(spec: StackSpec, infra_repo: Path) -> list[dict]:
    return [
        {"name": "stack-config", "hostPath": {"path": spec.config_dir(), "type": "Directory"}},
        {"name": "run", "emptyDir": {}},
    ]


CORE = [
    Component("opencode", _opencode_container, _opencode_volumes),
    Component("litellm", _litellm_container, _litellm_volumes),
    Component("squid", _squid_container, _squid_volumes),
]
COMPONENT_NAMES = {c.name for c in CORE}

REPO_CONFIG_FILE = "veggies.yml"
REPO_CONFIG_KEYS = {"model", "components"}


def resolve_components(names: list[str] | None) -> list[Component]:
    """Component names -> registry entries (spec order preserved)."""
    if names is None:
        return list(CORE)
    by_name = {c.name: c for c in CORE}
    unknown = sorted(set(names) - COMPONENT_NAMES)
    if unknown:
        raise ValueError(
            f"unknown components: {', '.join(unknown)} "
            f"(available: {', '.join(sorted(COMPONENT_NAMES))})"
        )
    return [by_name[n] for n in names]


def parse_repo_config(text: str) -> tuple[dict, list[str]]:
    """Validate veggies.yml content (schema v0). Returns (config, warnings);
    unknown keys warn, bad values raise."""
    data = yaml.safe_load(text)
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        raise ValueError(f"{REPO_CONFIG_FILE}: top level must be a mapping")
    warnings = [f"{REPO_CONFIG_FILE}: ignoring unknown key {k!r}"
                for k in sorted(set(data) - REPO_CONFIG_KEYS)]
    cfg: dict = {}
    if "model" in data:
        if not isinstance(data["model"], str):
            raise ValueError(f"{REPO_CONFIG_FILE}: 'model' must be a string")
        cfg["model"] = data["model"]
    if "components" in data:
        comps = data["components"]
        if not isinstance(comps, list) or not all(isinstance(c, str) for c in comps):
            raise ValueError(f"{REPO_CONFIG_FILE}: 'components' must be a list of strings")
        resolve_components(comps)  # raises on unknown names
        cfg["components"] = comps
    return cfg, warnings


def load_repo_config(repo: Path) -> tuple[dict, list[str]]:
    """Local convenience wrapper (missing file = empty config)."""
    f = repo / REPO_CONFIG_FILE
    if not f.is_file():
        return {}, []
    return parse_repo_config(f.read_text())

# Golden-stable volume ordering (tests/golden/pod.yaml is byte-compared).
VOLUME_ORDER = ["repo", "stack-config", "agent-config", "opencode-home", "tmp", "run"]


# --- Pod assembly ------------------------------------------------------------------


def render_pod(
    spec: StackSpec, infra_repo: Path, components: list[Component] | None = None
) -> list[dict]:
    """The multi-document kube YAML (PVCs + Pod) for one stack. All hostPath
    values are paths on the host the stack runs on (ADR 0014)."""
    if components is None:
        components = resolve_components(spec.components)
    containers = [c.render(spec, infra_repo) for c in components]
    vols: dict[str, dict] = {}
    for c in components:
        for v in c.volumes(spec, infra_repo):
            vols.setdefault(v["name"], v)
    ordered = [vols[n] for n in VOLUME_ORDER if n in vols]
    ordered += [v for n, v in vols.items() if n not in VOLUME_ORDER]

    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": spec.pod,
            "labels": {"app": "veggies", "veggies.io/stack": spec.name},
        },
        "spec": {
            "restartPolicy": "Always",
            "containers": containers,
            "volumes": ordered,
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

    docs = [pvc(v["persistentVolumeClaim"]["claimName"]) for v in ordered
            if "persistentVolumeClaim" in v]
    return docs + [pod]


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


def container_names(spec: StackSpec, components: list[Component] | None = None) -> list[str]:
    """kube play prefixes container names with the pod name."""
    components = components if components is not None else CORE
    return [f"{spec.pod}-{c.name}" for c in components]
