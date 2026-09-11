---
status: accepted
date: 2026-09-11
---

# 0041. Discussion elaboration: a persona POV roster answers /elaborate

## Context and problem statement

Issue #33 / discussion #32 converged on agents *participating* in
discussions, not only distilling them into issues (0038): a design
thread wants several expert reads of the same conversation, in public,
where the humans are talking. MVP scope: a trusted `/elaborate` comment
summons a roster of role agents, and each posts its own point of view
back on the discussion.

## Decision drivers

- The thread is the context, unchanged from 0038: the kick reuses the
  same REST fetch and budgets (4000 post / 2000 per comment / 12000
  total); nothing new to verify.
- Deliberateness: the trigger is a conscious act by a trusted author
  (OWNER/MEMBER/COLLABORATOR) - the same gate as 0038, still the whole
  injection boundary on this public repo.
- Personas must NOT mutate anything themselves: the envelope is
  deny-over-ask (0031), and the single mutation point is the kicked
  session - the only actor holding a token.
- GitHub is the record (0035): the deliverable is discussion comments,
  not state files, branches, or issues.

## Decision

`agent-trigger.yml` fires on `discussion_comment` (created) for a
trusted `/elaborate` exactly as for `/opencode` - same job, same gate.
The job exports `DISCUSSION_COMMAND` (`distill` default; `elaborate`
when the verb appears - `/elaborate` wins if one comment carries both
verbs) and `scripts/stack_kick.py` routes on it.

Elaborate mode kicks ONE session titled `D#<n> elaborate: <title>` whose
prompt embeds the whole fetched thread (0038's fetch + budgets). The
session dispatches one task subagent per persona from the new vendored
roster in `agent-config/agents/` - domain-expert, infra-architect,
marketer, seller, cto - each `mode: subagent` with `edit: deny` and
`bash: deny`: read-only POV producers that return text and nothing else
(the house subagent pattern, 0036). The session then posts exactly one
GraphQL `addDiscussionComment` per persona back on the discussion -
discussions reject `addComment` (verified live,
[0039](0039-discussion-feedback-contract.md)) - each body starting
`**<Role> POV**` so attribution is skimmable. If a post is
denied, the session names the missing token permission in its final
message and stops - the same degrade as 0038. No branch, no commit, no
PR, no issues, no labels: the comments are the deliverable.

One-shot semantics per comment, mirroring 0038's discussion stance: no
done-guard, and re-commenting `/elaborate` re-runs the roster. The issue
flow (`/opencode` on issues, the `agent-task` label) is untouched.
Proactive (uncalled) participation was raised in discussion #32 and is
explicitly deferred past MVP - the roster speaks only when spoken to.

One session fans out to one task subagent per persona rather than one
workflow job per persona. Rejected: one job per persona - 5x runner and
stack churn for zero isolation gain, since personas are read-only.
Rejected: personas posting their own comments - breaks the
single-mutation-point rule; they hold `bash: deny` precisely so posting
stays in the kicked session. Rejected: proactive chipping-in without
being called - deferred past MVP by the thread.

## Consequences

- Positive: a discussion gets five expert lenses in minutes, in public,
  with attribution a reader can skim; zero new long-lived processes; the
  workflow side stays GITHUB_TOKEN-only, and the POV comments ride the
  stack's bot PAT exactly like 0038's summary comment.
- Accepted: the vendored roster registers at stack boot (0019) - a
  `veggies up` is required after merge before `/elaborate` works.
- Accepted: the bot PAT needs `Discussions: write` for the POV comments
  (TODO(verify) - the PAT's grants live with the operator, and the
  runbook already lists sibling permissions pending, same posture as
  0038).
- Accepted: personas pin `litellm/kimi-k3` in frontmatter - today the
  stack default; a stack that re-points its default model keeps the
  roster on kimi-k3 until the roster files change.
- Negative / accepted: five subagent calls per `/elaborate` cost tokens
  even on thin threads; the deliberate-comment gate is the throttle.

## Links

- Extends [0038](0038-discussion-triggered-issue-distillation.md) (same
  trigger, fetch, and comment path; distill becomes one of two modes);
  comment mutation of [0039](0039-discussion-feedback-contract.md)
  (discussions take `addDiscussionComment`, never `addComment`);
  trigger semantics of [0035](0035-one-shot-labels-and-done-guard.md)
  (no done-guard on discussions); roster convention of
  [0019](0019-agent-rosters-and-skills.md); permission envelope of
  [0031](0031-no-ask-anywhere.md); subagent fan-out of
  [0036](0036-always-on-critic-for-kicked-sessions.md); kick mechanics of
  [0033](0033-issue-triggered-agent-kicks.md).
- Issue: <https://github.com/Innoptech/veggies/issues/33>
- Discussion: <https://github.com/Innoptech/veggies/discussions/32>
