---
status: proposed
date: 2026-09-15
---

# 0068. Kick routing across nodes: central runner pool, per-repo kick owner, requester attribution

> **Proposed - team review gate.** Part of the control-plane set 0064-0069.

## Context and problem statement

[0033](0033-issue-triggered-agent-kicks.md) kicks a repo's stack from
GitHub Actions on self-hosted runners that share the host with the stack
(`host.containers.internal:<port>`, an egress dport exception for the
runner user). With one node per user
([0066](0066-isolation-unit-one-vm-per-user.md)) the runner and the
target stack no longer share a host, several users may run stacks for
the same repo, and per-user spend needs to know who asked.
[0034](0034-session-observability.md)'s `#N: <issue>` session titles and
[0048](0048-agent-kick-install-as-one-module.md)'s per-repo agent-kick
block (`stack_host`, `stack_port`, `stack_password`) are the levers.

## Decision drivers

- One place for runner credentials; no RAM tax on every user node.
- A kick must land on a deterministic node.
- Attribution of a kick to a human without breaking `veggies costs`'s
  title parsing or the 0051 spend-log contract.
- No new inbound listeners; the tailnet is the path.

## Considered options

- Runners on every user node, labelled by login.
- **Central runner pool on the main node; a repo's kicks target its
  kick-owner's node over the tailnet** (chosen).
- Wake-on-kick (start a stopped node from the workflow).

## Decision outcome

1. **Runners stay one pool** (`github_runner` role) on `veggies-main`.
2. Each repo's agent-kick block sets `stack_host` to the **kick owner's**
   node tailnet IP (IP, not name: runner DNS is denied by design) and
   `stack_port` to that stack's port; the kick owner is the repo grant
   marked `kick_owner` in the control plane
   ([0067](0067-control-plane-veggies-core-web-app.md)), exactly one per
   repo. The runner user's egress gains a
   `100.64.0.0/10:4096-4196` exception; the headscale ACL lets
   `tag:main-node` reach every node's stack ports
   ([0064](0064-innoptech-tailnet-headscale-dex-github-oidc.md)).
3. **Attribution**: the kick comment names the requester ("requested by
   @login"; `TRIGGER_ACTOR = github.event.sender.login` for label events);
   every kick prompt mandates a `Requested-by: <login>` git trailer and a
   PR-body line (commits are authored by the App, 0063). **Per-user spend
   is per node**: `veggies costs --json` per node, summed by the app.
4. Attach instructions become tailnet URLs (`veggies ui` prints the URL;
   `--tunnel` keeps `ssh -L`).
5. A kick to a **stopped** node fails loudly with a comment (the 0034 skip
   feedback); wake-on-kick is deferred.

## Consequences

- Positive: runner credentials and the App key for registration stay on
  one host; adding a user adds no runners; every kick names its human.
- Negative / accepted: kicks and repo CI depend on the main node being up;
  per-requester money split is deferred (the session list API exposes no
  requester field); a stopped node means a failed kick until started.
- On acceptance: 0033, 0034, 0048 -> `(amended by 0068)`.

## Pros and cons of the options

### Runners per user node

- Good, because each node is self-sufficient.
- Bad, because +4 GB RAM and runner credentials on every node, N times the
  registration surface.

### Central pool + kick owner (chosen)

- Good, because one credential locus, deterministic routing, minimal
  change to the 0033 path.
- Bad, because the main node is a dependency for kicks; accepted.

### Wake-on-kick

- Good, because a stopped node never fails a kick.
- Bad, because the workflow would need a GCP credential and minutes of
  runner time per wake; a later ADR if the idle policy makes it painful.

## Links

- 0033, 0034, 0048, 0051, 0063, 0064, 0066, 0067;
  `.github/workflows/agent-trigger.yml`, `scripts/stack_kick.py`,
  `terraform/github/agent_kicks.tf`.
