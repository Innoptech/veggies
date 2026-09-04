---
status: accepted
date: 2026-09-04
---

# 0023. Capability model: the stack depends on contracts, not tools

## Context and problem statement

ADR 0016's Component was a rendering seam ("how do I draw YAML"), not a
capability seam ("what do I provide"). Wiring knowledge was scattered and
concrete: opencode's env imported squid's port constant and litellm's secret
layout; secret knowledge lived in three places (StackSpec naming,
render_secret_docs keys, cmd_up's values); cmd_attach and cmd_status were
opencode-concrete; write_stack_config knew every component's config files.
Swapping any implementation meant editing its consumers.

## Decision outcome

The stack depends on capability contracts; components depend on the
contracts and a PodContext they receive - never on each other.

- **Capabilities**: `harness` (agent runtime with attachable HTTP API),
  `model-router` (OpenAI-compatible gateway), `egress` (forward proxy).
  `orchestrator` is a reserved name with zero implementations until ADR 0017
  is refined.
- **Contracts** (cli/capabilities.py): `ServiceRef` (what a provided
  capability looks like: base_url, secret name, env hints), `SecretSpec`
  (declared secrets with sources: Generated | VaultKey), and the Component
  v2 protocol: `provides`, `requires`, `render(spec, ctx)`,
  `volumes(spec, ctx)`, `secrets(spec)`, `config_files(spec, infra_repo)`,
  `probes(spec)`, `attach(spec, record)`.
- **PodContext** assigns ports and hands out ServiceRefs; render order is
  topological over `requires`. Port numbers become private to their owning
  component.
- **Registry** maps capability -> implementation. veggies.yml v1 selects by
  capability key (`harness: opencode`, `model_router: litellm`); the v0
  `components:` list remains valid.
- **One implementation per capability today.** The seam permits a future
  second harness (e.g. claude-code via a thin shim server we would own -
  exact SDK surface TODO(verify) at that time); nothing about it is built
  or promised now. The seam is proven by a test-only stub harness.

## Consequences

- Positive: new components and swaps touch exactly one file; cmd_up,
  write_stack_config, status, and attach are fully generic; secret drift
  between three hand-maintained spots is structurally impossible.
- Negative: more protocol machinery around a 3-component stack; mitigated
  by keeping protocols data-shaped (dicts in/out), no class hierarchies.

## Links

- ADR 0013 (stack model), ADR 0016 (substrate/stack boundary)
- Proposed: ADR 0017 (orchestrator), ADR 0018 (MCPs), ADR 0019 (rosters)
