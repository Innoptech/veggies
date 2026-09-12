---
status: accepted (amended by 0043)
date: 2026-09-11
---

# 0040. Self-trigger guard: bot exclusion, command-anchored keyword, in-flight done-guard

## Context and problem statement

Verified 2026-09-11 on issue #33 (workflow runs 34603739921 kick,
34604254532 re-kick): the agent's *mandated* plan comment (ADR 0036) -
posted as `olgam4`, an org MEMBER - discussed the `/opencode` feature and
contained the literal keyword. Both trigger gates passed
(`contains(body, '/opencode')` and the OWNER/MEMBER/COLLABORATOR
association), so the workflow kicked a second session for the same issue
five minutes into the first. The done-guard (0035) could not help: the
issue was open and no `agent/issue-N` PR existed yet. The first session
had to be aborted by hand.

Two structural holes plus one race window:

1. The association gate trusts the bot account. Everything the agent does
   on GitHub rides the bot PAT; agent comments can never be allowed to
   trigger. (`github-actions[bot]` comments carry association NONE and
   were never a risk - the PAT identity was.)
2. `contains` treats prose mentions as commands. Issues about this very
   feature mention `/opencode` constantly.
3. The done-guard sees only finished work (closed issue, existing PR);
   the in-flight window between kick and PR is unguarded, so ANY stray
   trigger double-books the issue.

## Decision

- **Bot exclusion**: both comment triggers require
  `comment.user.login != 'olgam4'`. Hardcoded in the workflow on purpose:
  an Actions variable fails *open* when unset, and the identity is public
  record (the vault `github_user` key stays the source for credentials).
- **Command-anchored keyword**: `contains` becomes
  `startsWith(comment.body, '/opencode')` on both comment triggers. The
  command must be the first text; mentions, quotations, and "please
  /opencode this" no longer fire.
- **In-flight done-guard**: `stack_kick.py` gains `inflight_reason()` -
  a *busy* stack session titled `#<N>: ` skips the kick with exit 3
  ("session already working this issue"). Issue mode only (discussions
  keep their no-guard design, 0038). API failure degrades to proceeding,
  same posture as the 0035 guard.

## Consequences

- Positive: the agent structurally cannot retrigger the loop; prose about
  the feature is safe in any comment; the double-book race is closed for
  every trigger kind, present and future.
- Negative / accepted: `/opencode` mid-comment no longer triggers - the
  command convention is "first text of the comment", matching how it has
  been used so far. The bot account cannot kick by comment at all; its
  deliberate retriggers go through a human re-adding `agent-task`.
- Amends the `contains` trigger semantics of
  [0033](0033-issue-triggered-agent-kicks.md) and
  [0038](0038-discussion-triggered-issue-distillation.md); extends the
  done-guard of [0035](0035-one-shot-labels-and-done-guard.md) with the
  in-flight state.
