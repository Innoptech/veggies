"""Capability contracts for veggies stacks (ADR 0023).

The stack depends on these contracts; components (cli/components/*) depend
on them and on the PodContext they receive at render time - never on each
other. Pure data + typing only: no IO, no imports of concrete components.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

OPENCODE_PORT_BASE = 4096  # host ports allocated from here, first free

# Remote mode (ADR 0014): everything runs as this user over ssh+sudo.
REMOTE_USER = "stacks"
REMOTE_STATE_ROOT = f"/home/{REMOTE_USER}/.local/state/veggies"

# Shared securityContext for every component unless it opts out with cause.
HARDENED = {
    "readOnlyRootFilesystem": True,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
}


# --- Spec ----------------------------------------------------------------------


@dataclass
class StackSpec:
    name: str
    repo: str  # absolute path (mount) or clone URL (clone)
    mode: str = "mount"  # mount | clone
    port: int = OPENCODE_PORT_BASE
    host: str | None = None  # None = local; else ssh host alias (ADR 0014)
    model: str | None = None  # litellm alias, from veggies.yml or --model
    components: list[str] | None = None  # component names (v0); None = defaults
    selections: dict[str, str] | None = None  # capability -> impl (v1, ADR 0023)
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


# --- Secret declarations ---------------------------------------------------------


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


def secret_env(name: str, secret: str, key: str) -> dict:
    return {
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": secret, "key": key}},
    }


# --- Service wiring + observability contracts --------------------------------------


@dataclass(frozen=True)
class ServiceRef:
    """What a provided capability looks like to its consumers (ADR 0023)."""
    capability: str
    base_url: str
    secret: str | None = None  # podman secret holding its credentials
    secret_key: str | None = None  # key inside that secret holding the credential
    env: dict[str, str] = field(default_factory=dict)  # env vars consumers should set


@dataclass(frozen=True)
class StatusProbe:
    """One line of `veggies status` output from a component. kind=http hits
    the harness endpoint (harness auth); kind=exec runs exec_argv inside the
    probe-owning component's container and parses stdout as JSON."""
    label: str
    extract: Callable[[object], str]  # parsed JSON -> display value
    http_path: str | None = None
    exec_argv: tuple[str, ...] | None = None

    @property
    def kind(self) -> str:
        return "exec" if self.exec_argv else "http"


@dataclass(frozen=True)
class BuildSpec:
    """How the runtime materializes this component's image at up-time:
    built from a Containerfile (path relative to the infra repo) or pulled
    as-is (containerfile=None). Components own their images (ADR 0023);
    `ensure_images` builds/pulls exactly the selected components' images."""
    image: str
    containerfile: str | None = None  # relative to infra repo; None = pull only


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
    (sourced centrally); config_files() renders the stack's config dir;
    probes() feeds `veggies status`; attach() describes how the CLI attaches
    to a harness (argv template; None = not attachable).
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
    probes: Callable[[StackSpec], list[StatusProbe]] = lambda spec: []
    attach: Callable[[str, str], list[str]] | None = None  # (url, password) -> argv
    build: BuildSpec | None = None  # None = no image needed at up-time
