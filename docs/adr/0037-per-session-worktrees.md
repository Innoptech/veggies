---
status: accepted
date: 2026-09-11
---

# 0037. Per-session git worktrees inside the shared clone

## Context and problem statement

Every session on a stack - kicked via ADR 0033, supervised via ADR 0028, or
attached by a human - shares the one checkout mounted at `/workspace`.
Parallel sessions then overwrite each other's files and clobber each
other's git state. Observed live 2026-09-11 while working issue #27: with
three agent sessions active in the same clone, one session's
freshly-created branch vanished mid-task and the shared checkout switched
branches under it; two earlier sessions had already improvised ad-hoc
worktrees under `/tmp`.

## Decision drivers

- The kick path is HTTP-only (`scripts/stack_kick.py` on an ephemeral GHA
  runner; no host shell, no podman socket), so worktree creation cannot be
  orchestrated from outside - the kick prompt is the one guaranteed
  delivery channel.
- The permission envelope (0029/0031) denies `external_directory` outside
  the session dir except `/tmp/**`, and `ask` is banned: worktrees must
  live inside `/workspace` or under `/tmp`.
- `/tmp` is a pod `emptyDir`: a pod restart wipes it, losing unpushed work
  and leaving branches locked to dead paths.
- Observability (0034) hardcodes `?directory=/workspace`; keeping the
  session directory there keeps `veggies ui`/`sessions` and the workflow's
  deep links unchanged.
- Boring over clever (AGENTS.md rule 7): git worktrees are the standard
  tool; no new daemons, no new API surface.

## Considered options

- **A: per-session worktree at `/workspace/.veggies/wt/issue-N`, mandated
  by the kick prompt** (chosen).
- B: one pod per issue - heavyweight, burns ports/secrets, contradicts the
  long-lived stack model (0033).
- C: worktrees under `/tmp` - wiped on pod restart (emptyDir).
- D: sessions scoped to the worktree via `?directory=` - the worktree must
  exist *before* the session is created, which the HTTP-only kick cannot
  do; it also makes `/workspace` itself external to the session, breaking
  `git -C /workspace worktree add` bootstrap and all 0034 deep links.

## Decision outcome

Option A. `scripts/stack_kick.py`'s prompt mandates a mechanical
bootstrap: fetch, `git worktree add --lock --reason ... -B agent/issue-N
/workspace/.veggies/wt/issue-N origin/main`, exclude `/.veggies/` via
`.git/info/exclude`, then work only inside the worktree using absolute
paths (opencode's file tools resolve relative paths against the session
dir `/workspace`, not the shell's cwd). `-B` gives deterministic
fresh-or-reset semantics AND refuses ("already used by worktree") when
another live session holds the branch - the concurrency tripwire; the
prompt's fallback is a `-2` suffix, never `-f`/`--force` (which overrides
the tripwire) and never removing someone else's worktree. `--lock` makes
a live session's tree refuse a bare `worktree remove --force` (verified
2026-09-11, git 2.54: a locked worktree needs the deliberate double
`-f -f`). Exclusion is two-layer: `/.veggies/` is committed to THIS repo's
`.gitignore` (our own clones' hygiene), and `veggies up` writes the same
line to any stack repo's `.git/info/exclude` (`ensure_worktree_exclude`,
both modes, local and remote, common-dir aware) because mutating a mounted
repo's tracked `.gitignore` would be wrong. The session directory stays
`/workspace`.

## Consequences

- Positive: parallel sessions can no longer clobber each other's files or
  branch state; file-level isolation with zero permission-envelope changes
  and zero new runtime machinery. Side benefit: pytest's golden test breaks
  when the checkout path IS `/workspace` (path-substitution collision);
  sessions now run from `/workspace/.veggies/wt/...`, so that collision
  disappears for all kicked agents.
- Negative / accepted: prompt-driven, not enforced - an agent that ignores
  the bootstrap can still clobber the shared checkout (the envelope cannot
  deny writes inside the session dir). Enforcement would need an opencode
  plugin hook; deferred as unproven complexity.
- Accepted: a crashed run leaves a stale worktree holding the branch; the
  re-kick lands on the `-2` suffix, which the done-guard's
  `head=agent/issue-N` PR lookup (0035) does not match - the issue-closed
  half of the guard still applies. Cleanup subtlety: `git worktree remove`
  does NOT delete the branch - it survives with any unpushed commits, and
  the next kick's `-B` then resets it to `origin/main`, discarding them.
  Rescue work BEFORE removing a stale tree (`git branch backup-N
  agent/issue-N`); the runbook says so. Clone-mode `down --purge` removes
  the whole clone, worktrees included.
- Mount mode: worktrees live inside the operator's own repo (excluded from
  status by the same `info/exclude` line); `down --purge` never touches a
  mounted repo, so cleanup stays manual there. The exclusion also means
  casual `git status` hides the worktrees - `git worktree list` shows them
  (runbook §9).

## Links

- Issue: #27. Builds on: [0029](0029-deny-over-ask-permission-envelope.md),
  [0033](0033-issue-triggered-agent-kicks.md),
  [0034](0034-session-observability.md),
  [0035](0035-one-shot-labels-and-done-guard.md).
