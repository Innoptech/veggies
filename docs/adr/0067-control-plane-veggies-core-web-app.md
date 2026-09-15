---
status: proposed
date: 2026-09-15
---

# 0067. Control plane "veggies core": a thin web app that drives nodes through the CLI

> **Proposed - team review gate.** Part of the control-plane set 0064-0069.

## Context and problem statement

[0025](0025-stack-control-plane.md) tried a third-party control plane and
[0028](0028-retire-canvas-own-the-critic-loop.md) retired it, concluding
that opencode's own web UI plus `veggies supervise` cover daily use and
that a thin own dashboard ("Option C") is reconsidered only if that proves
insufficient. The driver changed: with one node per user
([0066](0066-isolation-unit-one-vm-per-user.md)) and GitHub-org login
([0064](0064-innoptech-tailnet-headscale-dex-github-oidc.md)), users need
a place to log in, see their own stacks, sessions and spend, enrol their
devices, edit stack config and start their node - none of which a
per-stack opencode UI can give. 0028's lesson stands: no second session
store, no podman socket in a container.

## Decision drivers

- Multi-user self-service without giving users ssh to nodes.
- Reuse the CLI as the only owner of stack definitions (0013/0016).
- The app must hold no vault password and no podman socket.
- Boring stack, consistent with the repo (Python, pytest, quadlets).
- Node lifecycle mutation stays reviewed tofu; the app only flips power.

## Considered options

- Extend opencode's UI (no multi-user concept upstream).
- Another third-party control plane (the niche collapsed, 0028).
- **A thin Python web app that shells into nodes through the existing
  CLI** (chosen).

## Decision outcome

1. **Stack**: Python 3.13, FastAPI + uvicorn, Jinja2 server-rendered pages
   (plain forms; HTMX vendored only if needed), `authlib` for OIDC, stdlib
   `sqlite3` (no ORM), stdlib `subprocess`. New top-level `core/` package
   (`cli/` stays stdlib+PyYAML; `core/` may import `cli/costs.py`, never
   the reverse). Image `deploy/images/core.Containerfile` bakes `cli/`,
   `agent-config/` and `deploy/` so renders match the operator's CLI;
   `state.json` records the rendering `cli_ref`. Runs as a rootless quadlet
   under the `core` user on `veggies-main`, `ReadOnly=true`, secrets via
   `Secret=` (Dex client secret, session secret, ssh key, headscale API
   key), published on loopback behind Caddy.
2. **Identity**: OIDC client of Dex; role `admin` iff login is in the
   `veggies_admins` group_vars list; every other org member is `user`.
3. **v1 scope**: Me (role, node, devices; "Add device" mints a single-use
   one-hour headscale pre-auth key for the user; list/expire own devices);
   Stacks and Sessions per node (`veggies ls/status/sessions --json`,
   live-first, attach URL + owner-only password reveal); Spend per node
   (`veggies costs --json`, per-issue rows); Admin: Users/roles, Repo
   grants (app-local table, exactly one `kick_owner` per repo), All nodes.
   v1.5: stack config editing through an app-owned override file
   (`<state_root>/<name>/veggies.override.yml`; precedence CLI flag >
   override > repo `veggies.yml` > default) with `up`/`sync` as background
   jobs; node start/stop with an idle timer (no busy session for
   `idle_minutes`) and optional schedule.
4. **How it drives nodes**: ssh as `veggies-core` then `sudo -n -u stacks`
   through the CLI's own `host_run` (0066); no podman socket, no vault
   password, no unrestricted sudo. GCP start/stop through the main node's
   attached service account limited to `veggies-u-*`
   ([0065](0065-cloud-substrate-gcp-compute-engine.md)). Node creation
   and deletion are never the app's.
5. **Authoritative state**: roster -> tofu `user_nodes`; identity ->
   GitHub via Dex at login; roles -> group_vars; tailnet nodes and keys ->
   headscale; stacks -> node-side `state.json`; grants, jobs, audit,
   overrides -> the app's SQLite; spend -> node `spend.jsonl*`.
6. **Repo grants are app-local** (admin grants who may run stacks for
   which repo and who is its kick owner). GitHub collaborator permission
   is an advisory check later, not the source of truth: it answers "may
   read", not "may spend the model budget".

## Consequences

- Positive: users self-serve within their own node; the operator stops
  being the only pair of hands; every action is audited; the CLI remains
  the single renderer.
- Negative / accepted: a web app to maintain (kept thin: server-rendered,
  no ORM, no JS build); the app's ssh key reaches every node as `stacks`
  (bounded by `from=`, the sudoers rule, and audit); version skew between
  the baked CLI and an operator's checkout is visible, not prevented.
- On acceptance: 0028 -> `(amended by 0036, 0067)`; 0025's "Option C"
  is taken with the multi-user driver recorded here.

## Pros and cons of the options

### Extend opencode's UI

- Good, because zero new surface.
- Bad, because upstream has no users, roles, nodes or spend; a fork is
  worse than a thin app.

### Third-party control plane

- Good, because someone else builds the UI.
- Bad, because 0028 documents the collapse of that niche and the
  two-session-stores problem; nothing maintained fits.

### Thin own web app (chosen)

- Good, because the scope is exactly ours and every write path is a CLI
  call we already test.
- Bad, because we own a web app forever; scope discipline is the answer.

## Links

- 0013, 0016, 0025, 0028, 0051, 0064, 0065, 0066, 0068; `core/` (to be
  written), runbook "veggies core" (to be written).
