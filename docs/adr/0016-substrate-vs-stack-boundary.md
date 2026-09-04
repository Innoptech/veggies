---
status: accepted
date: 2026-09-04
---

# 0016. Ansible is the substrate; the CLI is the stack

## Context and problem statement

After ADR 0013/0014 the same stack was defined twice: ansible roles
(`opencode`, `litellm`) installed host-global instances of the very
containers the `veggies` CLI renders into per-repo pods, with separate
config sync, auth, and observability paths. Two sources of truth for one
product, and the host ones did not know stacks existed (e.g. backups missed
`/home/stacks`). As the stack grows (MCPs, agent rosters, an orchestrator),
every new component would have to be added in both places.

## Decision outcome

The boundary is drawn at the pod line:

- **Ansible (substrate)** may only prepare a box so stacks can run there:
  `base`, `crowdsec`, `tailscale`, `podman`, `egress` (incl. per-user policy
  for the `stacks` account), `github_runner` (repo CI), `backup`. No ansible
  role may install, configure, or observe a stack container.
- **The CLI (the product)** owns 100% of stack definition: `roles/opencode`
  and `roles/litellm` are deleted; their useful content already lives in the
  CLI renderer (ADR 0013). The CLI grows a component model (`cli/stack.py`:
  a stack is a list of components; `core` = opencode+litellm+squid) so
  future MCPs/agents/orchestrator (ADRs 0017-0019) plug in without a
  monolith.
- **Per-repo declaration**: an optional `veggies.yml` in the target repo
  customizes the stack (schema v0: `model`, `components`); CLI flag >
  veggies.yml > default.
- **Observability**: `veggies status` reads container health + the opencode
  HTTP API per stack, identical locally and over ssh; the host-global
  `agent-status`/`agent-attach` scripts are deleted with the role.
- **Backups** repoint to the stacks state root
  (`/home/stacks/.local/state/veggies`); podman volumes are a known gap
  tracked by proposed ADR 0021.

## Consequences

- Positive: one source of truth for the stack; host provisioning and stack
  lifecycle evolve independently; new components land in one place.
- Negative: no host-global opencode for ad-hoc debugging (spin up a throwaway
  stack instead); stack volumes are not yet backed up (ADR 0021).

## Links

- ADR 0013, ADR 0014 (stack model this boundary serves)
- Proposed: ADR 0017, 0018, 0019, 0020, 0021, 0022
