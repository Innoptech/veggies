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

OPENCODE_PORT_BASE = 4096  # host ports allocated from here, first free
# Pod-internal ports are private to their component (ADR 0023): consumers
# discover services through PodContext, never by importing constants.

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
http_port {_SQUID_PORT}

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


def render_opencode_json(infra_repo: Path, router_base_url: str,
                         model: str | None = None) -> str:
    """Stack variant of agent-config/opencode.json: router address injected
    by the caller (ADR 0023) and the master key via env (secretKeyRef)
    instead of an auth.json file."""
    src = json.loads((infra_repo / "agent-config/opencode.json").read_text())
    provider = src["provider"]["litellm"]
    provider["options"]["baseURL"] = router_base_url
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


# --- Secret declarations (ADR 0023) ----------------------------------------------
# Components declare their secrets; cmd_up sources values generically.


@dataclass(frozen=True)
class Generated:
    """Random value, generated at up-time."""
    nbytes: int


@dataclass(frozen=True)
class VaultKey:
    """Sourced from the vault (secrets/model.yml) at up-time."""
    key: str


@dataclass(frozen=True)
class SecretSpec:
    """One podman secret: veggies-<stack>-<name_suffix> with these keys."""
    name_suffix: str
    keys: dict[str, Generated | VaultKey]


@dataclass(frozen=True)
class ServiceRef:
    """What a provided capability looks like to its consumers (ADR 0023)."""
    capability: str
    base_url: str
    secret: str | None = None      # podman secret holding its credentials
    env: dict[str, str] = field(default_factory=dict)  # env vars consumers should set


@dataclass
class PodContext:
    """Wiring handed to every component at render time: spec, repo, and
    service discovery. Components read dependencies from here - never from
    each other's modules (ADR 0023)."""
    spec: StackSpec
    infra_repo: Path
    providers: dict  # capability -> Component
    components: list = field(default_factory=list)

    def service(self, capability: str) -> ServiceRef:
        provider = self.providers.get(capability)
        if provider is None or provider.service_ref is None:
            raise ValueError(
                f"stack has no provider for capability {capability!r} "
                f"(have: {sorted(self.providers)})")
        return provider.service_ref(self.spec)


@dataclass(frozen=True)
class Component:
    """One member of a stack pod (ADR 0023 capability seam).

    provides/requires declare the capability wiring; render/volumes describe
    the container via the PodContext; secrets() declares podman secrets
    (sourced centrally); config_files() renders the stack's config dir.
    volumes()/config_files() keys are merged by name across components;
    first declaration wins."""

    name: str
    provides: str
    requires: tuple[str, ...]
    render: Callable[[PodContext], dict]
    volumes: Callable[[PodContext], list[dict]]
    service_ref: Callable[[StackSpec], ServiceRef | None] = lambda spec: None
    secrets: Callable[[StackSpec], list[SecretSpec]] = lambda spec: []
    config_files: Callable[[PodContext], dict[str, str]] = lambda ctx: {}


_OPENCODE_CONTAINER_PORT = 4096


def _opencode_container(ctx: PodContext) -> dict:
    spec = ctx.spec
    publish_ip = "0.0.0.0" if spec.host else "127.0.0.1"
    router = ctx.service("model-router")
    egress = ctx.service("egress")
    return {
        "name": "opencode",
        "image": IMAGE_OPENCODE,
        # opencode writes instance state (.gitignore etc.) into
        # ~/.config/opencode at bootstrap (EROFS 500s on every API call if
        # read-only, verified 2026-09-04) - so stack-config mounts at
        # /stack-config and the wrapper copies opencode.json into the
        # writable home volume before exec'ing the server.
        "command": ["sh", "-c"],
        "args": [
            "mkdir -p /root/.config/opencode && "
            "cp /stack-config/opencode.json /root/.config/opencode/ && "
            # Vendored agents/skills (if shipped) copy alongside; global
            # config dirs are where opencode discovers them.
            "cp -r /stack-config/agents /root/.config/opencode/ 2>/dev/null; "
            "cp -r /stack-config/skills /root/.config/opencode/ 2>/dev/null; "
            f"exec opencode serve --hostname 0.0.0.0 --port {_OPENCODE_CONTAINER_PORT}"
        ],
        "workingDir": "/workspace",
        "env": [
            _secret_env("OPENCODE_SERVER_PASSWORD", spec.secret_opencode, "password"),
            _secret_env("LITELLM_MASTER_KEY", router.secret, "master_key"),
        ] + [{"name": k, "value": v} for k, v in egress.env.items()],
        "ports": [
            {
                "containerPort": _OPENCODE_CONTAINER_PORT,
                "hostPort": spec.port,
                "hostIP": publish_ip,
            }
        ],
        "volumeMounts": [
            {"name": "repo", "mountPath": "/workspace"},
            # Directory mounts only - subPath file mounts bypass SELinux
            # relabeling and read as EACCES (verified 2026-09-04).
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
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
            "exec": {"command": ["sh", "-c", f"nc -z 127.0.0.1 {_OPENCODE_CONTAINER_PORT} || exit 1"]},
            "initialDelaySeconds": 10,
            "periodSeconds": 30,
        },
    }


def _opencode_volumes(ctx: PodContext) -> list[dict]:
    repo_path = ctx.spec.repo  # clone mode: CLI clones first, repo is the clone path
    return [
        {"name": "repo", "hostPath": {"path": repo_path, "type": "Directory"}},
        {
            "name": "opencode-home",
            "persistentVolumeClaim": {"claimName": ctx.spec.volume_opencode},
        },
        {"name": "tmp", "emptyDir": {}},
    ]


_LITELLM_PORT = 4000  # pod-internal only, never published


def _litellm_container(ctx: PodContext) -> dict:
    spec = ctx.spec
    return {
        "name": "litellm",
        "image": IMAGE_LITELLM,
        "args": ["--config", "/agent-config/config.yaml", "--port", str(_LITELLM_PORT), "--host", "0.0.0.0"],
        "env": [
            _secret_env("LITELLM_MASTER_KEY", spec.secret_litellm, "master_key"),
            _secret_env("LITELLM_SALT_KEY", spec.secret_litellm, "salt_key"),
            _secret_env("FIREWORKS_API_KEY", spec.secret_litellm, "fireworks_api_key"),
            # litellm calls providers through the egress proxy too: on the
            # VPS the per-UID nftables rules drop anything else (ADR 0006).
        ] + [{"name": k, "value": v} for k, v in ctx.service("egress").env.items()],
        "volumeMounts": [
            {"name": "agent-config", "mountPath": "/agent-config", "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": MEMORY_LIMITS["litellm"]}},
        "securityContext": HARDENED,
        "livenessProbe": {
            "exec": {"command": ["sh", "-c",
                                 f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {_LITELLM_PORT}), 3)\" || exit 1"]},
            "initialDelaySeconds": 15,
            "periodSeconds": 30,
        },
    }


def _litellm_volumes(ctx: PodContext) -> list[dict]:
    # Local: live-mount the vendored config (edit + `veggies up` to apply).
    # Remote: a rendered copy sits in the stack's config dir on that host.
    spec = ctx.spec
    litellm_cfg = (
        spec.config_dir() if spec.is_remote
        else str(ctx.infra_repo / "agent-config" / "litellm")
    )
    return [
        {"name": "agent-config", "hostPath": {"path": litellm_cfg, "type": "Directory"}},
    ]


_SQUID_PORT = 3128  # pod-internal only, never published


def _squid_service_ref(spec: StackSpec) -> ServiceRef:
    # busybox/toybox tools only honor the lowercase forms - ship both.
    proxy = f"http://127.0.0.1:{_SQUID_PORT}"
    return ServiceRef(
        capability="egress",
        base_url=proxy,
        env={
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
    )


def _squid_container(ctx: PodContext) -> dict:
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
            "exec": {"command": ["bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{_SQUID_PORT} || exit 1"]},
            "initialDelaySeconds": 5,
            "periodSeconds": 30,
        },
    }


def _squid_volumes(ctx: PodContext) -> list[dict]:
    return [
        {"name": "stack-config", "hostPath": {"path": ctx.spec.config_dir(), "type": "Directory"}},
        {"name": "run", "emptyDir": {}},
    ]


def _opencode_secrets(spec: StackSpec) -> list[SecretSpec]:
    return [SecretSpec("opencode", {"password": Generated(12)})]


def _opencode_config_files(ctx: PodContext) -> dict[str, str]:
    infra_repo = ctx.infra_repo
    files = {"opencode.json": render_opencode_json(
        infra_repo, model=ctx.spec.model,
        router_base_url=ctx.service("model-router").base_url)}
    # Vendored agents + skills ship as per-stack copies (edit + `veggies up`
    # to apply; the opencode wrapper copies them into its global config dir).
    for sub in ("agents", "skills"):
        src = infra_repo / "agent-config" / sub
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    files[f"{sub}/{f.relative_to(src)}"] = f.read_text()
    return files


def _litellm_secrets(spec: StackSpec) -> list[SecretSpec]:
    return [SecretSpec("litellm", {
        "master_key": Generated(32),
        "salt_key": Generated(32),
        "fireworks_api_key": VaultKey("fireworks_api_key"),
    })]


def _litellm_service_ref(spec: StackSpec) -> ServiceRef:
    return ServiceRef(
        capability="model-router",
        base_url=f"http://127.0.0.1:{_LITELLM_PORT}/v1",
        secret=f"{spec.pod}-litellm",
    )


def _litellm_config_files(ctx: PodContext) -> dict[str, str]:
    if not ctx.spec.is_remote:
        return {}  # local: agent-config/litellm is live-mounted instead
    # No infra checkout on the VPS: ship the litellm config as a copy.
    return {"config.yaml": (ctx.infra_repo / "agent-config/litellm/config.yaml").read_text()}


def _squid_config_files(ctx: PodContext) -> dict[str, str]:
    return {"squid.conf": render_squid_conf(), "allowlist.txt": render_allowlist()}


CORE = [
    Component("opencode", "harness", ("model-router", "egress"),
              _opencode_container, _opencode_volumes,
              secrets=_opencode_secrets, config_files=_opencode_config_files),
    Component("litellm", "model-router", ("egress",),
              _litellm_container, _litellm_volumes,
              service_ref=_litellm_service_ref,
              secrets=_litellm_secrets, config_files=_litellm_config_files),
    Component("squid", "egress", (),
              _squid_container, _squid_volumes,
              service_ref=_squid_service_ref, config_files=_squid_config_files),
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


def build_context(
    spec: StackSpec, infra_repo: Path, components: list[Component] | None = None
) -> PodContext:
    """Resolve components and their capability wiring (ADR 0023)."""
    if components is None:
        components = resolve_components(spec.components)
    providers = {}
    for c in components:
        if c.provides in providers:
            raise ValueError(f"two components provide {c.provides!r}")
        providers[c.provides] = c
    ctx = PodContext(spec=spec, infra_repo=infra_repo, providers=providers,
                 components=components)
    for c in components:  # validate requires against providers up front
        for req in c.requires:
            if req not in providers:
                raise ValueError(f"{c.name} requires {req!r}; stack lacks it")
    return ctx


def render_pod(
    spec: StackSpec, infra_repo: Path, components: list[Component] | None = None
) -> list[dict]:
    """The multi-document kube YAML (PVCs + Pod) for one stack. All hostPath
    values are paths on the host the stack runs on (ADR 0014)."""
    ctx = build_context(spec, infra_repo, components)
    containers = [c.render(ctx) for c in ctx.components]
    vols: dict[str, dict] = {}
    for c in ctx.components:
        for v in c.volumes(ctx):
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


def required_secret_values(
    spec: StackSpec, components: list[Component] | None = None
) -> dict[str, Generated | VaultKey]:
    """Flat key -> source map for every declared secret (keys must be unique
    across the stack's components)."""
    out: dict[str, Generated | VaultKey] = {}
    for c in (components if components is not None else resolve_components(spec.components)):
        for s in c.secrets(spec):
            for key, source in s.keys.items():
                if key in out:
                    raise ValueError(f"secret key collision across components: {key}")
                out[key] = source
    return out


def secret_names(spec: StackSpec, components: list[Component] | None = None) -> list[str]:
    """All podman secret names for the stack (declaration-derived)."""
    return [
        f"{spec.pod}-{s.name_suffix}"
        for c in (components if components is not None else resolve_components(spec.components))
        for s in c.secrets(spec)
    ]


def render_secret_docs(
    spec: StackSpec, values: dict[str, str], components: list[Component] | None = None
) -> list[dict]:
    """K8s Secret docs for one stack, from component declarations. Only ever
    passed to kube play via stdin at up-time - never written to disk
    (ADR 0013). Name-sorted for determinism."""
    docs = []
    for c in (components if components is not None else resolve_components(spec.components)):
        for s in c.secrets(spec):
            docs.append({
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": f"{spec.pod}-{s.name_suffix}"},
                "data": {k: _b64(values[k]) for k in s.keys},
            })
    return sorted(docs, key=lambda d: d["metadata"]["name"])


def container_names(spec: StackSpec, components: list[Component] | None = None) -> list[str]:
    """kube play prefixes container names with the pod name."""
    components = components if components is not None else CORE
    return [f"{spec.pod}-{c.name}" for c in components]
