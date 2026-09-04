---
status: accepted
date: 2026-09-04
---

# 0015. Project identity renamed: garden -> veggies

## Context and problem statement

The project outgrew its working name. The operator renamed it from `garden`
to `veggies`. This ADR records the scope and the deliberate non-renames so
future readers are not confused by old names in old records.

## Decision outcome

Renamed:

- The CLI: `garden` -> `veggies` (`cli/veggies.py`, `mask veggies-install`,
  `~/.local/bin/veggies`).
- Everything the CLI creates: pod/secret/volume prefix `veggies-<name>`,
  labels `app=veggies` and `veggies.io/stack`, quadlet files
  `veggies-<name>.kube`, the `veggies-watchdog` units, state dir
  `~/.local/state/veggies` (remote: `/home/stacks/.local/state/veggies`),
  env vars `VEGGIES_*`.
- The host identity: ansible inventory host, terraform ovh resource names and
  default instance name, tailscale hostname, and the operator's ssh alias are
  `veggies` (the VPS was never converged, so this is a free rename).
- The litellm provider display name in agent-config/opencode.json.

Explicitly NOT renamed:

- ADRs 0000-0014 and their prose: decided ADRs are immutable history.
- The remote service account `stacks` (a role-level name, not project
  identity).
- The workspace directory `~/Innoptech/veggie` (singular; no absolute-path
  references exist in the repo).

## Legacy policy

Clean break. Podman secrets/volumes cannot be renamed, so no migration
machinery: old stacks are purged with the old CLI, old dirs removed, stacks
recreate fresh under `veggies up`. The CLI prints a one-time hint when it
finds a legacy `~/.local/state/garden` directory.

## Consequences

- Positive: one consistent identity; no dual-name compatibility surface.
- Negative: any running `garden-*` stacks must be recreated (only the
  disposable smoke stack existed at rename time).

## Links

- ADR 0013, ADR 0014 (retain the old name as historical records)
