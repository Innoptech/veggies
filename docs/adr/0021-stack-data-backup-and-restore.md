---
status: proposed
date: 2026-09-04
---

# 0021. Stack data backup and restore

> **Proposed, needs refining.** Context and open questions only; nothing here
> is decided. Do not implement against this ADR.

## Context

After ADR 0016 the backup role covers `/home/stacks/.local/state/veggies`
(stack metadata) but not podman volumes — and `veggies-<name>-opencode`
volumes hold the actual session history. Losing a volume loses the agent's
memory of the repo.

## Open questions

- Export mechanism: host-side `podman volume export` systemd timer vs an
  in-pod restic sidecar? Host-side keeps secrets out of pods (ADR 0013
  posture) but needs the backup role to discover stack volumes.
- Restore UX: `veggies restore <name>`? How does restore interact with
  secrets that were never backed up (state.json holds passwords; podman
  secrets do not)?
- Reuse the existing restic repo/encryption from the backup role or a
  separate repository per host?
- Retention policy per stack vs global.
- Scope: litellm/squid state is disposable (no DB since ADR 0011); only
  opencode-home volumes + state.json matter. Confirm nothing else accretes
  state (TODO(verify) as components land).
