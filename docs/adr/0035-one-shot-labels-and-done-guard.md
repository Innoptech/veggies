---
status: accepted (amended by 0046)
date: 2026-09-11
---

# 0035. One-shot labels and the done-guard

## Context and problem statement

First live trigger run (2026-09-11): issue #15 got kicked while its work
was already sitting committed in the stack clone - nothing stopped a
re-kick of a done issue, and the `agent-task` label stayed on afterwards,
so "which issues are handled" had no readable answer.

## Decision drivers

- Re-kicking a done issue burns a session and risks duplicate branches/PRs.
- The label must mean something stable: on = "wants the agent", off =
  "handled". A label that never clears is noise.
- The guard must never block a kick on its own failure (a GitHub API
  hiccup is not a reason to stop work).

## Decision

`agent-task` is a **one-shot trigger**: the workflow deletes it after a
successful kick *or* a skip; only a failure keeps it (the intent survives
a down stack). Re-adding the label is the deliberate retrigger.

Before kicking, `scripts/stack_kick.py` runs the **done-guard**: with a
GitHub token in env (the workflow's own GITHUB_TOKEN; `pull-requests:
read` joined `issues: write`), skip with exit code 3 when the issue is
closed or an `agent/issue-N` PR already exists in any state (open = in
flight, merged = shipped). The workflow turns exit 3 into a skip comment
naming the reason, not a job failure. Guard API errors degrade to
proceeding; a missing token downgrades the guard to a stderr warning
(manual kicks are the operator's call).

Rejected: tracking handled-ness in a state file on the VPS (the PR *is*
the record - GitHub is the source of truth) and skipping quietly (a
trigger that does nothing must say why, on the issue).

## Consequences

- The issue list is self-cleaning: done issues lose the label and carry a
  comment saying what happened (session link, skip reason, or failure).
- Manual `stack_kick.py` runs get the same guard when GH_TOKEN is present.
- A re-kick after a push-denied run still works: no PR exists yet, so the
  guard passes and the session finds the committed branch in the clone.

## Links

- Amends the trigger semantics of [0033](0033-issue-triggered-agent-kicks.md);
  comments via [0034](0034-session-observability.md)
