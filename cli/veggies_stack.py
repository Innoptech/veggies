"""veggies stack assembly: registry, repo config, pod composition (ADR 0023).

Contracts live in cli/capabilities.py; implementations in cli/components/*.
This module owns the registry (capability -> implementations), veggies.yml
parsing, and composing components into kube YAML. Pure renderers only:
nothing here touches podman, ssh, the network, or the vault.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))  # cli/ on path

from capabilities import (  # noqa: E402  (re-exported for cli/veggies.py)
    HARDENED,
    OPENCODE_PORT_BASE,
    REMOTE_STATE_ROOT,
    REMOTE_USER,
    Component,
    Generated,
    PodContext,
    SecretSpec,
    ServiceRef,
    StackSpec,
    StatusProbe,
    VaultKey,
    allocate_port,
    sanitize_name,
    secret_env,
    state_dir,
)
from components import litellm, opencode, orchestrator, squid  # noqa: E402

# Re-exported for tests and cli/veggies.py (single import surface).
IMAGE_LITELLM = litellm.IMAGE_LITELLM
IMAGE_OPENCODE = opencode.IMAGE_OPENCODE
IMAGE_SQUID = squid.IMAGE_SQUID
SQUID_ALLOWLIST_BASE = squid.SQUID_ALLOWLIST_BASE
SQUID_MODEL_ENDPOINTS = squid.SQUID_MODEL_ENDPOINTS
render_allowlist = squid.render_allowlist
render_squid_conf = squid.render_squid_conf
render_opencode_json = opencode.render_opencode_json


# --- Registry (ADR 0023) -----------------------------------------------------------

REGISTRY: dict[str, dict[str, Component]] = {
    "harness": {"opencode": opencode.COMPONENT},
    "model-router": {"litellm": litellm.COMPONENT},
    "egress": {"squid": squid.COMPONENT},
    "orchestrator": {"builtin": orchestrator.COMPONENT},  # opt-in (ADR 0017)
}
# Iteration order of DEFAULT_SELECTION pins the container order (golden-stable).
DEFAULT_SELECTION = {"harness": "opencode", "model-router": "litellm", "egress": "squid"}
# veggies.yml capability keys -> capability name. "orchestrator" is
# selectable in the file so it fails with the polite reserved-capability
# error (ADR 0023 / proposed ADR 0017) instead of an "unknown key" warning.
CAPABILITY_KEYS = {"harness": "harness", "model_router": "model-router",
                   "egress": "egress", "orchestrator": "orchestrator"}

CORE = [REGISTRY[cap][impl] for cap, impl in DEFAULT_SELECTION.items()]
COMPONENT_NAMES = {c.name for c in CORE}


def resolve_components(
    names: list[str] | None = None, selections: dict[str, str] | None = None
) -> list[Component]:
    """veggies.yml wiring (ADR 0023). v0 `components:` picks by component name
    (and is exclusive with capability keys); otherwise capability selections
    over DEFAULT_SELECTION."""
    if names is not None and selections:
        raise ValueError("use either 'components' or capability keys "
                         "(harness/model_router/egress), not both")
    if names is not None:
        by_name = {c.name: c for impls in REGISTRY.values() for c in impls.values()}
        unknown = sorted(set(names) - by_name.keys())
        if unknown:
            raise ValueError(
                f"unknown components: {', '.join(unknown)} "
                f"(available: {', '.join(sorted(by_name))})")
        return [by_name[n] for n in names]
    merged = {**DEFAULT_SELECTION, **(selections or {})}
    for cap, impl in merged.items():
        if cap not in REGISTRY:
            raise ValueError(f"unknown capability {cap!r} "
                             f"(known: {', '.join(sorted(REGISTRY))})")
        if not REGISTRY[cap]:
            raise ValueError(f"capability {cap!r} is reserved but has no "
                             "implementation yet (proposed ADRs 0017-0019)")
        if impl not in REGISTRY[cap]:
            raise ValueError(f"unknown {cap} implementation {impl!r} "
                             f"(available: {', '.join(sorted(REGISTRY[cap]))})")
    return [REGISTRY[cap][impl] for cap, impl in merged.items()]


# --- veggies.yml (schema v1) ---------------------------------------------------------

REPO_CONFIG_FILE = "veggies.yml"
REPO_CONFIG_KEYS = {"model", "components", *CAPABILITY_KEYS}


def parse_repo_config(text: str) -> tuple[dict, list[str]]:
    """Validate veggies.yml content (schema v1). Returns (config, warnings);
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
        resolve_components(names=comps)  # raises on unknown names
        cfg["components"] = comps
    selections = {CAPABILITY_KEYS[k]: data[k] for k in CAPABILITY_KEYS if k in data}
    if selections:
        for key in CAPABILITY_KEYS:
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"{REPO_CONFIG_FILE}: {key!r} must be a string")
        resolve_components(selections=selections)  # raises on unknown impl/reserved
        if "components" in cfg:
            raise ValueError(f"{REPO_CONFIG_FILE}: use either 'components' or "
                             "capability keys, not both")
        cfg["selections"] = selections
    return cfg, warnings


def load_repo_config(repo: Path) -> tuple[dict, list[str]]:
    """Local convenience wrapper (missing file = empty config)."""
    f = repo / REPO_CONFIG_FILE
    if not f.is_file():
        return {}, []
    return parse_repo_config(f.read_text())


# --- Pod assembly --------------------------------------------------------------------

# Golden-stable volume ordering (tests/golden/pod.yaml is byte-compared).
VOLUME_ORDER = ["repo", "stack-config", "agent-config", "opencode-home", "stack-state", "tmp", "run"]


def build_context(
    spec: StackSpec, infra_repo: Path, components: list[Component] | None = None
) -> PodContext:
    """Resolve components and their capability wiring (ADR 0023)."""
    if components is None:
        components = resolve_components(spec.components, spec.selections)
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


def container_names(spec: StackSpec, components: list[Component] | None = None) -> list[str]:
    """kube play prefixes container names with the pod name."""
    components = components if components is not None else resolve_components(
        spec.components, spec.selections)
    return [f"{spec.pod}-{c.name}" for c in components]


# --- Secrets (declared per component, sourced centrally) ----------------------------


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def required_secret_values(
    spec: StackSpec, components: list[Component] | None = None
) -> dict[str, Generated | VaultKey]:
    """Flat key -> source map for every declared secret (keys must be unique
    across the stack's components)."""
    out: dict[str, Generated | VaultKey] = {}
    for c in (components if components is not None
              else resolve_components(spec.components, spec.selections)):
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
        for c in (components if components is not None
                  else resolve_components(spec.components, spec.selections))
        for s in c.secrets(spec)
    ]


def render_secret_docs(
    spec: StackSpec, values: dict[str, str], components: list[Component] | None = None
) -> list[dict]:
    """K8s Secret docs for one stack, from component declarations. Only ever
    passed to kube play via stdin at up-time - never written to disk
    (ADR 0013). Name-sorted for determinism."""
    docs = []
    for c in (components if components is not None
              else resolve_components(spec.components, spec.selections)):
        for s in c.secrets(spec):
            docs.append({
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": f"{spec.pod}-{s.name_suffix}"},
                "data": {k: _b64(values[k]) for k in s.keys},
            })
    return sorted(docs, key=lambda d: d["metadata"]["name"])
