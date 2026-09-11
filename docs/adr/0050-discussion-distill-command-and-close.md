---
status: accepted
date: 2026-09-11
---

# 0050. Discussion distill command renamed to /distill; distill closes the discussion as resolved

## Context and problem statement

Issue #67: one verb named two missions. `/opencode` on an issue meant
"work this issue to a PR" (0033); the same `/opencode` on a discussion
meant "mine this thread into issues" (0038) - a single overload
disambiguated only by event type. The same issue carries the operator
side-note: a distilled discussion left open lingers as apparent
undelivered work - the issues exist, but the thread still reads like an
open request.

## Decision drivers

- One verb, one mission: with three commands on the surface, the
  event-type overload is the confusing one; a rename is cheaper than
  remembering it.
- The close is part of the distill's deliverable, so it must follow the
  results, never precede them - 0039's split (workflow owns acks, the
  agent owns results) decides who closes.
- No new trust surface: the close rides the same bot-PAT grant
  (`Discussions: write`) the summary comment already needs (0038).

## Decision

The discussion distill trigger is renamed `/opencode` -> `/distill`.
The workflow gate's discussion branch is now
`startsWith '/distill' || startsWith '/elaborate'` - the parens are
load-bearing: without them, `&&`/`||` precedence parses the pair as
`(discussion && /distill) || (/elaborate && trusted)`, and `/elaborate`
would fire on ANY comment event, issues included. The issue branch keeps
`/opencode`. The distill ack comment announces the close ("...comments
the issue links below when done; if the thread yielded issues it then
closes this discussion as resolved"), and the rc=3 skip hint is event-aware (discussions: "Comment
/distill (or /elaborate) again..."; issues unchanged). Docs (the ADR
index, runbook, architecture, AGENTS.md item 9) move to the new verb in
the same change.

Close-after-distill is agent-side, step 5 of the distill kick prompt:
ONLY IF the summary comment actually landed AND at least one issue was
created, the agent closes the discussion via GraphQL `closeDiscussion`
with the default reason (RESOLVED, verified in the schema 2026-09-11),
reusing the node id fetched for the comment. A denied comment or a
zero-issue conversational thread leaves the discussion open. Best effort
on the bot PAT, exactly like the comment: if denied, the agent names
`Discussions: write` in its final message and stops.

**The close is a completion signal, not a trigger gate.** Closing a
discussion does not lock it - closed discussions still accept comments -
so 0038's no-done-guard stance stands: a fresh `/distill` on a closed
discussion re-kicks, with the prompt-level dedupe against existing
issues as the duplicate control. Nor can the close self-trigger: the
workflow subscribes to `issues(labeled)`, `issue_comment(created)`, and
`discussion_comment(created)` only - the `discussion.closed` event is
not consumed, so the agent's own close cannot re-fire the job (the same
shape of analysis as 0043's self-kick bound).

0043's hygiene bound widens in the same change: all three kick-prompt
templates (issue, distill, elaborate) now forbid the agent from starting
a comment with any of the three verbs - `/opencode`, `/distill`,
`/elaborate` - pinned by pytest.

This change is also the recorded command-surface extension pattern for
the next verb: a new discussion command is the workflow gate + the
`DISCUSSION_COMMAND` mapping + the prompt hygiene line + the pytest
binding in `tests/test_stack_kick.py`. This ADR is the template.

Rejected: closing from the workflow after a successful kick. The job's
rc=0 means "session queued", not "issues created" - a workflow-side
close would stamp RESOLVED before the deliverable exists and invert
0039's contract (workflow owns acks; the agent owns results, and the
close is part of the result).

Rejected: an alias period where `/opencode` keeps working on
discussions. Two verbs for one mission doubles the self-kick surface
0043 bounds, for zero user gain; the retired verb dies silently -
acceptable for a one-operator system, stated as a cost.

Accepted, restated for the new verb (0040's trade): prefix semantics
stay uniform across all three commands - a comment starting `/distilled`
fires too. The command is the first text of the comment; mid-comment
mentions never fire.

TODO(verify): whether `discussion_comment` (created) fires on a CLOSED
discussion - the re-kick-after-close stance above assumes it does.

## Consequences

- Positive: one verb per mission, and a distilled discussion ends
  visibly done. The close can never fail silently alone: it rides the
  same `Discussions: write` grant the summary comment already needs, and
  a denial of that grant is already 0038's named-permission degrade.
- Positive: the verbs now own the discussion lifecycle - `/elaborate`
  keeps the room open (POVs invite reply), `/distill` harvests and
  closes it.
- Negative / accepted: muscle memory for `/opencode` on discussions dies
  without an alias (see above); the ack and skip-hint texts teach the
  new verb at the moment of use.
- Accepted: the close is distill-only machinery - pytest pins
  `closeDiscussion` out of the issue and elaborate prompts.

## Links

- Amends [0038](0038-discussion-triggered-issue-distillation.md) (the
  trigger verb; the no-done-guard and prompt-level dedupe stance
  stands); extends the prompt-hygiene list of
  [0043](0043-interim-shared-identity-trigger.md); command anchoring of
  [0040](0040-self-trigger-guard.md); feedback contract of
  [0039](0039-discussion-feedback-contract.md).
- Issue: <https://github.com/Innoptech/veggies/issues/67>
