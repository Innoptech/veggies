---
status: accepted
date: 2026-09-11
---

# 0022. Cost metering and model routing

## Context and problem statement

Since the workflow switched from human/local coding to kicked agent
sessions, spend is a first-class operational number - and it is currently
invisible: "what did this PR cost as a whole?" has no answer today.
Discussion #38 is the demand signal.

The recorded conflict stands: litellm's spend tracking - the `/spend`
endpoints and per-key spend - is in-memory in this deployment (ADR 0011:
v1 has no spend DB, in-memory only) and zeroes on every `veggies down` /
`veggies up` / `veggies sync`. The historical path to a durable spend DB
is litellm's `DATABASE_URL`, and upstream has removed sqlite support, so
there is no cheap durable option to simply switch on.

## Decision drivers

- Spend is an operational number: durable, self-hosted, and answerable
  from the host without new long-lived services.
- One source of truth for cost; no parallel meters that can disagree.
- Attribution rides conventions already enforced (titled sessions,
  `agent/issue-N` branches), not a new metadata store.
- No new listeners, no new egress destinations, no spend data leaving the
  host.
- The VPS is ~12 GB RAM: every stateful addition competes with the stacks
  themselves.

## Decision

1. **Metering point: the in-pod litellm router, always.** It is already
   the single audited hop for every model call - author, persona/task
   subagents, adversarial review, supervisor judge (both judge paths POST
   to the loopback router: the ADR 0036 in-pod supervisor daemon from its
   own container, and `veggies supervise` via exec inside the litellm
   container) - per ADR 0011.
   There is no second metering path. Rejected: opencode's own per-session
   token stats as a source - they miss supervisor judge calls and would
   fork the source of truth.
2. **Substrate: per-call logs exported to a durable host file, rolled up
   at read time - no database.**
   - The file lives under the stack state root
     (`~/.local/state/veggies/<name>/` local,
     `/home/stacks/.local/state/veggies/<name>/` remote). The mount lands
     in `cli/components/litellm.py` as a plain hostPath - CLI-owned per
     ADR 0016, no ansible involvement, and no subPath mounts (verified
     broken here).
   - Size-based rotation; rotated segments are the long-period record.
   - Long-period durability rides the backup role's existing restic scope
     (`backup_paths: [/home/stacks/.local/state/veggies]` in
     `ansible/roles/backup/defaults/main.yml`) once backups un-gate per
     ADR 0024. `backup_enabled` is `false` today (in group_vars per ADR
     0024): restic is the target, not a live path. Local-workstation
     stacks have no backup at all, and `veggies down --purge` deletes
     cost logs with the state root.
   - The host file is **authoritative**; litellm's in-memory `/spend` is
     informational-only by design. The number that survives a restart
     lives in a file we own, not in the vendor's memory.
   - `TODO(verify)`: the exact litellm per-call log export mechanism on
     the pinned `v1.99.1` image - nothing in
     `agent-config/litellm/config.yaml` enables it today, and the
     container currently mounts no host path for logs (the live-mounted
     agent-config of decision 5 is config, not state). Resolved by the
     metering implementation (follow-up issue #46); upstream docs were
     unreachable when this ADR was written.
3. **Attribution: no new metadata store - the existing title/PR
   conventions are the axis.**
   - Sessions are titled `#N: <issue>` (ADR 0034) or, for
     discussion-phase work, `D#N: <discussion>` (ADR 0038; `D#N
     elaborate:` in ADR 0041); PRs follow `agent/issue-N` (ADR 0035).
     Cost-per-PR = the sum over every `#N:`-titled session: the initial
     kick, supervisor refinements (same session re-run, ADR 0036), and
     re-kicks (new sessions, ADR 0035).
   - Decided requirement (the correlation mechanism): **every router call
     carries its session title in litellm per-call metadata.** The
     opencode harness stamps its session title; the supervisor judge
     stamps the judged session's title. Neither stamp exists today -
     verified: `cli/components/opencode.py` wires only the apiKey, and
     neither judge path stamps metadata - `cli/supervisor.py`
     `JUDGE_EXEC_SCRIPT` (the `veggies supervise` path) posts only
     model/messages plus temperature/max_tokens, and the in-pod
     supervisor daemon's `judge()` (`deploy/supervisor/daemon.py`, the
     ADR 0036 path that judges kicked sessions) posts the same shape over
     pod loopback - so judge spend is currently unattributable. Both
     stamping changes land with the metering implementation (#46).
     `TODO(verify)`: both sides of the stamp - the exact litellm
     metadata/tags field and its presence in the exported per-call logs
     on `v1.99.1`, and the opencode harness stamping capability or hook
     on the pinned harness (its litellm wiring is a static rendered JSON
     from `cli/components/opencode.py` `render_opencode_json`; nothing
     in-tree demonstrates per-request metadata stamping on opencode
     1.18.27) - both resolved in #46. Fallback if no harness hook exists:
     timestamp-window correlation of router logs against the harness
     session API, reported at reduced confidence, with the gap visible in
     the unattributed bucket.
   - Boundary: `D#N` discussion-phase spend (elaborations, distillations)
     reports under the discussion, not the PR. Untagged or untitled spend
     (manual sessions, anything off-convention) lands in an explicit
     unattributed-overhead bucket that `veggies costs` shows - never
     silently absorbed. A trustworthy per-PR number requires a visible
     remainder.
   - Verified status quo, recorded: every in-pod consumer authenticates
     with the litellm master key (`cli/components/opencode.py`,
     `cli/components/supervisor.py`), so key-level attribution is
     impossible today and titles are the only axis. This narrows ADR
     0011's "revocable agent keys" consequence: while the master key is
     the only key, the pod boundary is the revocation boundary.
4. **Reporting surface: a `veggies costs` subcommand - CLI-first, not
   dashboard-first.** Per-PR, per-session, and since-date rollups computed
   at read time over the host file; output shows total vs attributed vs
   unattributed overhead. Spend data never leaves the host: no hosted
   dashboards, no SaaS cost tools (egress allowlist ADR 0006,
   no-new-listeners ADR 0024, master-key-never-leaves-the-pod ADR
   0028/0036). Implementation sequence: metering (#46), then `veggies
   costs` (#47), then a thin agent skill that shells out to the subcommand
   (#49) - no standalone cost agent; an agent answering cost questions
   burns the very thing it measures. The skill serves local/operator-side
   sessions: the CLI is operator-side (ADR 0014), so an in-pod agent
   cannot invoke `veggies costs` - for kicked sessions the number is
   surfaced by the workflow/operator, not from inside the pod.
5. **Fallback-chain declaration point: status quo ratified.** Routing
   policy lives in `agent-config/litellm/config.yaml`
   (`router_settings.fallbacks`), shipped by the CLI (live-mounted
   locally, rendered copy remotely - `cli/components/litellm.py`). No new
   `veggies.yml` `models:` section. Metering is per-call, so a session
   that spans fallback price points is priced per request, never per
   session-model.

### Deferred deliberately

- **Postgres-in-pod spend DB** - a stateful service (upgrades, backups,
  RAM) on a ~12 GB VPS mortgages the platform for a dashboard. Re-entry:
  the log-file rollup proves insufficient at retained volume.
- **Per-agent litellm virtual keys** - the default stays the per-stack
  master key. Re-entry: a demonstrated need for per-component spend splits
  the title axis cannot give.
- **Budget enforcement (`max_budget`)** - explicitly out of scope per the
  discussion author's follow-up: "We do not need a max_budget enforcement
  now, we need to see how much the pr cost as a whole." litellm budgets
  are per-key, so enforcement depends on per-agent keys first; on the
  master key it would be an all-or-nothing kill switch. Re-entry:
  `veggies costs` shows spend crossing an operator-set threshold, a
  second tenant/contributor on shared stacks, or any provider beyond
  Fireworks.
- **Dashboards/graphs** - re-entry: someone demonstrably reads the rollup
  and asks.

## Consequences

- Positive: one source of truth at the router; durable history with zero
  new long-lived processes; attribution falls out of conventions already
  enforced (0034/0035); the ledger is self-hosted end to end.
- Negative: rollup-at-read cost grows with log size; the title convention
  is now a load-bearing contract; long-period durability waits on the ADR
  0024 backup re-entry; local stacks and `down --purge` lose history.
- This ADR pre-answers ADR 0021's open scope question ("confirm nothing
  else accretes state") for one file: the metering log is new durable
  stack state, already inside the backup role's `backup_paths`.

## Links

- Discussion: <https://github.com/Innoptech/veggies/discussions/38> (the
  forcing thread). Follow-up issues: #46 (metering), #47 (`veggies
  costs`), #49 (agent skill shelling out to it).
- Router and mount ownership: [0011](0011-litellm-gateway-model-routing.md),
  [0016](0016-substrate-vs-stack-boundary.md); CLI placement:
  [0014](0014-remote-stacks-over-ssh.md); state scope:
  [0021](0021-stack-data-backup-and-restore.md); interim constraints:
  [0024](0024-interim-access-and-identity-constraints.md); key custody:
  [0028](0028-retire-canvas-own-the-critic-loop.md); conventions the
  attribution axis rides: [0034](0034-session-observability.md),
  [0035](0035-one-shot-labels-and-done-guard.md),
  [0036](0036-always-on-critic-for-kicked-sessions.md),
  [0038](0038-discussion-triggered-issue-distillation.md),
  [0041](0041-discussion-elaboration-persona-roster.md).
