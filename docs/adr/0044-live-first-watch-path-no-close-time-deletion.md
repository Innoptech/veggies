---
status: accepted
date: 2026-09-11
---

# 0044. Watch path lists live sessions first; no close-time session deletion

## Context and problem statement

Discussion #42 proposed killing opencode sessions when their issue or PR
closes. The thread rejected deletion and distilled issue #57: the real
pain is findability while issues are still open - one issue accumulates
several `#N:` kicked sessions (0034) plus anonymous interactive
attaches, and the watch path (`veggies ui` / `veggies sessions`)
rendered strictly newest-first, so live work scrolled under finished
rows. Cost was never the driver: an idle session is transcript rows on
disk - zero CPU, zero tokens.

## Decision drivers

- Live work must surface first regardless of history size; the watch
  path is the operate/demo path (0034).
- Never delete the only copy: stack backups are still proposed (0021),
  so a session transcript has no second home.
- Posted deep links never rot: 0034 comments a session link onto the
  issue at every kick; a deleted session turns each into a dead end.
- The `#N:` title stays the correlation key: 0034 verified the list API
  returns no session metadata, so there is nothing stronger to key on.

## Decision

**Live-first watch path.** One pure ordering helper,
`live_first(sessions, status)` in `cli/veggies.py`, feeds both renderers
(`session_links` for `cmd_ui`, `format_sessions` for `cmd_sessions`).
"Live" = present in `/session/status` with a type other than `idle` -
the same predicate the supervisor daemon uses
(`deploy/supervisor/daemon.py:136`); 0017 verified the endpoint lists
only non-idle sessions (absence = idle), so the CLI and the daemon never
disagree about the same session. Idle rows are capped at 10 by default -
one screen of the most-recently-touched idle (`IDLE_ROWS_DEFAULT`) - and
live rows are never hidden. `--all` lifts the cap; `--issue N` is never
capped. `cmd_ui` prints the header `sessions (live first):` with an
overflow footer pointing at `veggies sessions`; `format_sessions`
renders the hidden count as `... and N more idle sessions (use --all)`.

**No close-time session deletion, ever.** The thread's reasons:

- Audit trail: sessions are the record of what the agent did and what
  the critic injected (0034/0036).
- PR review happens against finished sessions; close time is exactly
  when the reviewer wants the transcript.
- Runbook §9's stale-worktree cleanup checks session ownership via the
  session list; deleting sessions destroys that record.
- The title is a mush correlation key, but it is the only one the API
  gives (0034); deleting rows makes correlation worse, not better.

Rejected:

- **Close-event reaper** (the #42 proposal). Failure modes walked
  through in the thread: closing an issue mid-re-kick kills the fresh
  session; a reopened issue has lost its history with no restore path;
  a stack unreachable at event time forces a choice between silent
  skips and a retry queue - both worse than the disease. And it solves
  a problem we do not have: idle sessions cost nothing.
- **Hiding idle sessions by default** instead of capping:
  recently-finished sessions must stay visible - PR review happens
  against them. Cap, don't hide.
- **Default grouping-by-issue**: `--issue N` is the gesture; renderer
  grouping is complexity for no daily win.
- **A prune command now**: any deletion is gated on 0021 backups ALONE
  (never delete the only copy) and stays decoupled from 0022 metering.
  After this change, prune is optional storage hygiene, not a
  watch-path fix.
- **Strengthening the correlation key** (sidecar index, upstream
  hooks): a substrate decision, deferred on the record.

## Recorded deferrals

- Busy/idle is an *activity* signal spent here as a *lifecycle* signal.
  The real question is "which session needs ME" (actionability): a
  session parked on a permission prompt renders busy (verified in
  `cmd_supervise`, 2026-09-09) though it is waiting for a human, and an
  attached-but-waiting interactive session renders idle though someone
  owns it. Actionability is deliberately deferred.
- The live group is title-blind: task-subagent sessions are listed
  (0017) and sort above their `#N:` parents during kicks. Intended -
  they are live work. Do not "fix" this with grouping.
- Browser boundary: the web UI's Home SPA is upstream's (1.18.27) and
  out of reach. Home groups Today/Yesterday/Older (runbook "Web UI
  map", verified 2026-09-11); the only recency signal the API exposes
  is the session's updated timestamp, and a dead session's never moves,
  so corpses sink out of the daily browser view within ~48h on their
  own. The residual browser wall is bounded, not solved.

## Consequences

- Positive: the watch path scales with history - live work is the first
  screen no matter how many finished sessions accumulate.
- Positive: live-never-hidden keeps runbook §9's worktree-ownership
  safety check sound (a live session can never be capped out of view;
  locked by `test_format_sessions_live_beyond_cap_still_shown`).
- Positive: no deletion means every posted session link keeps
  resolving.
- Negative / accepted: an attached-but-waiting interactive session is
  API-idle and capped together with finished ones - indistinguishable
  through the status endpoint. `--all` is the gesture; the runbook
  decodes "idle" once.

## Links

- Amends the watch path of [0034](0034-session-observability.md); the
  live predicate is shared with the supervisor daemon of
  [0036](0036-always-on-critic-for-kicked-sessions.md)
- References: [0017](0017-agent-orchestrator-and-workflows.md) (status
  endpoint verified), [0035](0035-one-shot-labels-and-done-guard.md),
  [0037](0037-per-session-worktrees.md),
  [0021](0021-stack-data-backup-and-restore.md),
  [0022](0022-cost-metering-and-model-routing.md)
- Issue #57; discussion #42
