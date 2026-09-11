"""veggies stack assembly: registry, repo config, pod composition.

Contracts live in cli/capabilities.py; implementations in cli/components/*.
This module owns the registry (capability -> implementations), veggies.yml
parsing, and composing components into kube YAML. Pure renderers only:
nothing here touches podman, ssh, the network, or the vault.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path, PurePosixPath

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))  # cli/ on path

from capabilities import (  # noqa: E402  (re-exported for cli/veggies.py)
    HARDENED,
    OPENCODE_PORT_BASE,
    REMOTE_PROXY,
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
from components import litellm, mcp_toolbox, opencode, squid  # noqa: E402
from components import supervisor as supervisor_component  # noqa: E402
from permission_envelope import (  # noqa: E402
    ask_violations_in_config,
    ask_violations_in_markdown,
    project_tier_files,
    scan_project_tier,
)

# Re-exported for tests and cli/veggies.py (single import surface).
IMAGE_LITELLM = litellm.IMAGE_LITELLM
IMAGE_OPENCODE = opencode.IMAGE_OPENCODE
HARNESS_BASE_IMAGE = opencode.HARNESS_BASE_IMAGE
IMAGE_SQUID = squid.IMAGE_SQUID
SQUID_ALLOWLIST_BASE = squid.SQUID_ALLOWLIST_BASE
SQUID_MODEL_ENDPOINTS = squid.SQUID_MODEL_ENDPOINTS
render_allowlist = squid.render_allowlist
render_squid_conf = squid.render_squid_conf
render_opencode_json = opencode.render_opencode_json


# --- Registry -----------------------------------------------------------

REGISTRY: dict[str, dict[str, Component]] = {
    "harness": {"opencode": opencode.COMPONENT},
    "model-router": {"litellm": litellm.COMPONENT},
    "egress": {"squid": squid.COMPONENT},
    # Opt-in only (ADR 0036): no DEFAULT_SELECTION entry, so default stacks
    # are unchanged; selected via the `supervision:` capability key or the
    # v0 `components:` list.
    "supervision": {"supervisor": supervisor_component.COMPONENT},
}
# Iteration order of DEFAULT_SELECTION pins the container order (golden-stable).
DEFAULT_SELECTION = {"harness": "opencode", "model-router": "litellm", "egress": "squid"}
# veggies.yml capability keys -> capability name.
CAPABILITY_KEYS = {"harness": "harness", "model_router": "model-router",
                   "egress": "egress", "supervision": "supervision"}

CORE = [REGISTRY[cap][impl] for cap, impl in DEFAULT_SELECTION.items()]
COMPONENT_NAMES = {c.name for c in CORE}

# Opt-in MCP sidecars (ADR 0018): name -> component. Additive on top of the
# capability selection; never part of DEFAULT_SELECTION.
MCP_REGISTRY: dict[str, Component] = {
    "toolbox": mcp_toolbox.COMPONENT,
}


def resolve_mcps(names: tuple[str, ...] | list[str] | None) -> list[Component]:
    """`mcps:` veggies.yml key -> components. Unknown names raise."""
    unknown = sorted(set(names or ()) - MCP_REGISTRY.keys())
    if unknown:
        raise ValueError(
            f"unknown mcps: {', '.join(unknown)} "
            f"(available: {', '.join(sorted(MCP_REGISTRY))})")
    return [MCP_REGISTRY[n] for n in (names or ())]


def stack_components(spec: StackSpec) -> list[Component]:
    """The full component list for a stack: capability selection + MCPs."""
    return resolve_components(spec.components, spec.selections) + resolve_mcps(spec.mcps)


def resolve_components(
    names: list[str] | None = None, selections: dict[str, str] | None = None
) -> list[Component]:
    """veggies.yml wiring. v0 `components:` picks by component name
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
                             "implementation yet")
        if impl not in REGISTRY[cap]:
            raise ValueError(f"unknown {cap} implementation {impl!r} "
                             f"(available: {', '.join(sorted(REGISTRY[cap]))})")
    return [REGISTRY[cap][impl] for cap, impl in merged.items()]


# --- veggies.yml (schema v1) ---------------------------------------------------------

REPO_CONFIG_FILE = "veggies.yml"
REPO_CONFIG_KEYS = {"model", "components", "mcps", "github", "harness_containerfile",
                    *CAPABILITY_KEYS}


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
    if "mcps" in data:
        mcps = data["mcps"]
        if not isinstance(mcps, list) or not all(isinstance(m, str) for m in mcps):
            raise ValueError(f"{REPO_CONFIG_FILE}: 'mcps' must be a list of strings")
        resolve_mcps(mcps)  # raises on unknown names
        cfg["mcps"] = tuple(mcps)
    if "github" in data:
        if not isinstance(data["github"], bool):
            raise ValueError(f"{REPO_CONFIG_FILE}: 'github' must be a bool")
        cfg["github"] = data["github"]
    if "harness_containerfile" in data:
        if not isinstance(data["harness_containerfile"], str):
            raise ValueError(f"{REPO_CONFIG_FILE}: 'harness_containerfile' must be a string")
        cfg["harness_containerfile"] = validate_overlay_path(data["harness_containerfile"])
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


def validate_overlay_path(raw: str) -> str:
    """The `harness_containerfile` value: a repo-relative path, normalized.
    Absolute paths and '..' escapes are rejected - the value only ever
    resolves against the repo root."""
    p = PurePosixPath(raw)
    if p.is_absolute():
        raise ValueError(f"{REPO_CONFIG_FILE}: 'harness_containerfile' must be "
                         f"repo-relative, got absolute path {raw!r}")
    if ".." in p.parts:
        raise ValueError(f"{REPO_CONFIG_FILE}: 'harness_containerfile' must not "
                         f"escape the repo ('..' in {raw!r})")
    normalized = str(p)  # PurePosixPath collapses './' and duplicate slashes
    if not raw.strip() or normalized in ("", "."):
        raise ValueError(f"{REPO_CONFIG_FILE}: 'harness_containerfile' must be a "
                         "non-empty repo-relative path")
    return normalized


def validate_overlay_containerfile(text: str, base: str = HARNESS_BASE_IMAGE) -> None:
    """Single-stage overlay rules: exactly one FROM, exactly the pinned
    harness base; no COPY/ADD - the build context is the state images dir,
    so repo files are unreachable (teach the pinned-fetch pattern instead).
    Raises ValueError with an actionable message."""
    # Join backslash-continuations into logical lines first, so a RUN ... \
    # whose continuation line starts with the word COPY is not a false hit.
    logical_lines: list[str] = []
    pending = ""
    for line in text.splitlines():
        line = pending + line
        if line.rstrip().endswith("\\"):
            pending = line.rstrip()[:-1] + " "
        else:
            logical_lines.append(line)
            pending = ""
    if pending:
        logical_lines.append(pending)
    instructions = []
    for line in logical_lines:
        line = line.strip()
        if not line or line.startswith("#"):  # comments / parser directives
            continue
        parts = line.split(None, 1)
        instructions.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
    for instr, _ in instructions:
        if instr in ("COPY", "ADD"):
            raise ValueError(
                f"overlay Containerfile: {instr} is not allowed - the build context "
                "contains no repo files, so there is nothing to copy from; fetch what "
                "you need in a RUN step with a version+sha256-pinned URL instead")
    froms = [rest for instr, rest in instructions if instr == "FROM"]
    if not froms:
        raise ValueError("overlay Containerfile must be FROM the pinned harness base "
                         f"{base!r} (found no FROM)")
    if len(froms) > 1:
        raise ValueError("overlay Containerfile: multi-stage overlays are not "
                         "supported (single FROM only)")
    if froms[0] != base:
        raise ValueError("overlay Containerfile must be FROM the pinned harness base "
                         f"{base!r}, got {froms[0]!r}")


def overlay_image_name(text: str) -> str:
    """Content-addressed overlay image ref. The tag names overlay CONTENT
    only (the FROM line inside it pins the base); the build rebuilds
    unconditionally every up/prepare, and a base flip busts it via
    buildah's parent-image-ID layer-cache keying - the rebuilt image
    re-takes this same tag."""
    return f"localhost/veggies-harness-overlay:{hashlib.sha256(text.encode()).hexdigest()[:16]}"


# --- Pod assembly --------------------------------------------------------------------

# Golden-stable volume ordering (tests/golden/pod.yaml is byte-compared).
VOLUME_ORDER = ["repo", "stack-config", "agent-config", "opencode-home", "stack-state", "tmp", "run"]


def build_context(
    spec: StackSpec, infra_repo: Path, components: list[Component] | None = None
) -> PodContext:
    """Resolve components and their capability wiring."""
    if components is None:
        components = stack_components(spec)
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
    values are paths on the host the stack runs on."""
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
              else stack_components(spec)):
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
                  else stack_components(spec))
        for s in c.secrets(spec)
    ]


def render_secret_docs(
    spec: StackSpec, values: dict[str, str], components: list[Component] | None = None
) -> list[dict]:
    """K8s Secret docs for one stack, from component declarations. Only ever
    passed to kube play via stdin at up-time - never written to disk.
    Name-sorted for determinism."""
    docs = []
    for c in (components if components is not None
              else stack_components(spec)):
        for s in c.secrets(spec):
            docs.append({
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": f"{spec.pod}-{s.name_suffix}"},
                "data": {k: _b64(values[k]) for k in s.keys},
            })
    return sorted(docs, key=lambda d: d["metadata"]["name"])
