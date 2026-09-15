---
status: proposed
date: 2026-09-15
---

# 0066. Isolation unit is one VM per user; the stack contract stays single-tenant per node

> **Proposed - team review gate.** Part of the control-plane set 0064-0069.

## Context and problem statement

veggies is single-operator by construction: one `stacks` Linux user owns
every pod on the host ([0013](0013-repo-scoped-agent-stacks.md),
[0014](0014-remote-stacks-over-ssh.md)), the stack registry `state.json`
and the vault password live on the operator's workstation, and stack names
are global across hosts (AGENTS.md rule 9). The requirement is now
**isolation**: each user runs their own stacks and cannot see or attach to
another user's sessions. Two shapes were on the table: per-Linux-user
multitenancy on a shared host, or one host per user.

## Decision drivers

- Isolation that is a real boundary (kernel + network), not a convention.
- Do not rewrite the stack model that works: single `stacks` user, ports
  from 4096, per-UID nftables, quadlets, per-stack secrets.
- Two drivers (operator CLI and the control plane) must not split-brain
  on ports and passwords.
- Drivers should not need the vault password.
- No throwaway code on a host being retired (OVH).

## Considered options

- Per-Linux-user multitenancy on a shared host (uid ranges, per-user state
  roots and port ranges, per-user sudo).
- **One VM per user**; everything inside a node unchanged (chosen).
- Run the veggies CLI on the node itself and drive it over ssh.

## Decision outcome

1. **The VM is the isolation unit.** Every node (`veggies-u-<login>`,
   [0065](0065-cloud-substrate-gcp-compute-engine.md)) keeps exactly
   today's layout: the single `stacks` user, `REMOTE_USER` /
   `REMOTE_STATE_ROOT` constants, ports allocated from 4096, the
   `egress_denied_users` loop, quadlets. No per-uid multitenancy is built,
   not even transitionally.
2. **The stack registry moves node-side**:
   `/home/stacks/.local/state/veggies/state.json` (0600, owner `stacks`)
   is authoritative for remote stacks; the CLI's `State` gains a host
   (read/write over ssh, atomic temp+`mv`). Local workstation stacks keep
   the local file. Stack names are unique **per node**; the "global across
   hosts" rule is retired and the app displays `<login>/<name>`.
3. **Node-local secrets**: in remote mode `resolve_secret_values` reads
   `VaultKey`s from an Ansible-delivered
   `/home/stacks/.config/veggies/secrets.yml` (new `agent_node` role,
   `no_log`) instead of the operator's vault, so `veggies up --host ...`
   needs no vault password. Ansible delivers a file and never touches a
   stack - the [0016](0016-substrate-vs-stack-boundary.md) boundary holds.
4. **Control-plane entry**: a `veggies-core` login user on every node
   with `authorized_keys` restricted by `from=<main tailnet IP>` and a
   sudoers drop-in `veggies-core ALL=(stacks) NOPASSWD: ALL` - the app
   reuses the CLI's existing `ssh ... sudo -n -u stacks` path
   ([0067](0067-control-plane-veggies-core-web-app.md)).
5. **Login -> node mapping** is the tofu `user_nodes` roster rendered into
   the generated inventory (`veggies_owner`) and the app's `nodes.yml`; a
   new user is a reviewed tofu PR, not self-service.
6. **OVH `veggies` is transitional**: the operator's stacks are recreated
   on `veggies-u-oli` (optional history carry-over = one
   `podman volume export/import`, documented, no tooling), then the OVH
   host leaves the inventory.

## Consequences

- Positive: isolation is a kernel + tailnet boundary; every ADR about the
  stack model stays true inside a node; no split-brain between drivers;
  the vault password stays with Ansible only.
- Negative / accepted: cost per user is a VM (idle stop in 0067); the CLI
  gains a node-side registry code path and a second secret source; stack
  history moves with the node, not the user.
- On acceptance: 0013, 0014, 0016 -> `(amended by 0066)`; AGENTS.md rule 9
  loses "names are global across hosts" and gains "vault only for local
  stacks".

## Pros and cons of the options

### Per-Linux-user multitenancy

- Good, because one host to pay for.
- Bad, because uid ranges, per-user state roots, port ranges, sudo rules
  and a runtime user-creation path are all new code with a soft boundary -
  and would be built on the host we are retiring.

### One VM per user (chosen)

- Good, because nothing inside a node changes and the boundary is the
  strongest available.
- Bad, because cost scales with users; accepted and mitigated.

### Run the CLI on the node

- Good, because a single code path for the app.
- Bad, because a moving infra checkout lands on every node and "which
  version rendered this pod" returns; revisit only if the ssh-driven
  remote path proves too chatty.

## Links

- 0013, 0014, 0016, 0065, 0067, 0068; `cli/veggies.py` (`State`,
  `resolve_secret_values`), `ansible/roles/agent_node/` (to be written).
