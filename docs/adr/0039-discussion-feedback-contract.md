---
status: accepted
date: 2026-09-11
---

# 0039. Discussion feedback: agent-owned results, workflow-owned acks

## Context and problem statement

ADR 0038 shipped discussion kicks with a recorded assumption: "GraphQL
`addComment(subjectId)` - the one mutation covers issues and discussions".
The first live discussion kick (workflow run 34602194185, 2026-09-11)
falsified it: the kick succeeded and the session distilled the thread into
issues #33/#34, but the session-link step failed - GitHub answers
`addComment` on a discussion subject with HTTP 200 plus an `errors[]`
payload (`addDiscussionComment` is the discussion mutation; schema
introspection shows them side by side). The same wrong mutation in the
kick prompt's closing instruction ate the agent's results comment too, so
the discussion got no feedback at all.

At the same time the product call changed: a session link on the
*discussion* is noise. The discussion's useful feedback is the outcome -
which issues were distilled from it.

## Decision

Feedback on a discussion kick is split by who can know what:

- The **workflow** posts a minimal ack on the discussion (one line:
  distilling, created issues will link back, workflow-log link) and owns
  the failure comment. It uses `addDiscussionComment` with the workflow's
  `GITHUB_TOKEN` (`discussions: write` is already in the job permissions).
- The **agent** posts the results: the closing comment listing the created
  issue links, via `addDiscussionComment` with the stack's bot PAT (best
  effort - the PAT's `Discussions: write` grant is operator-held, same
  posture as 0038's other PAT grants). Every created issue links the
  discussion in its `## Context` section (verified live on #33/#34), so
  GitHub's back-reference appears on the discussion even if the PAT comment
  is denied.
- The session-link comment becomes **issue-only**: issue kicks keep it
  unchanged (session id, deep link, watch path); discussion kicks get no
  session link anywhere.

Implementation: `agent-trigger.yml` carries both mutations and selects per
event type; `stack_kick.py`'s discussion prompt instructs
`addDiscussionComment` (asserted in `tests/test_stack_kick.py`).

## Consequences

- Positive: a discussion can no longer go silent - the ack is
  GITHUB_TOKEN-guaranteed, and failure feedback lands on the discussion
  too (previously it would have hit the same wrong mutation).
- Positive: the mutation selection is explicit per event type instead of a
  false universal claim; the workflow comment and the prompt assert the
  verified run id.
- Negative / accepted: two comment paths where 0038 wanted one. The
  subjects are different APIs; pretending otherwise is what broke.
- Amends [0038](0038-discussion-triggered-issue-distillation.md):
  replaces the `addComment`-covers-both claim and the session-link-back
  feedback; the trigger, thread budget, no-done-guard, and
  human-labels-first design stand.
