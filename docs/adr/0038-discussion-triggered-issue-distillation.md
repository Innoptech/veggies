---
status: accepted (amended by 0050)
date: 2026-09-11
---

# 0038. Discussion-triggered kicks: distill a discussion into issues

## Context and problem statement

Issue #23: "We want to be able to open a discussion, tag an agent and for
it to understand what we had been talking about and create the issues
along their plan, their happy path, their criterion of success." 0033 left
discussions unhandled ("GraphQL; follow-up if wanted") - this is the
follow-up. The trigger must reuse the 0033 event path (outbound-poll
runners, no inbound listener) and the kick must carry the *whole thread*:
the value is the conversation, not the opening post.

## Decision drivers

- The thread is the context. The `discussion_comment` webhook payload
  carries only the triggering comment, so the kick fetches the thread
  itself. Discussions read fine over REST (`GET
  /repos/{r}/discussions/{n}/comments`, verified 2026-09-11 against this
  repo) - 0033's GraphQL worry was outdated; only *writing* a discussion
  comment is GraphQL-only.
- Deliberateness: the trigger must be a conscious act by a trusted human,
  same as the issue path (this repo is now public - verified 2026-09-11,
  so the author-association gate is the whole injection boundary).
- Re-kicking an *evolving* discussion is the point, unlike re-kicking a
  done issue: no done-guard on discussions.
- GitHub is the record (0035): created issues link the discussion, the
  agent comments the issue links back, nothing lives in state files.

## Decision

`agent-trigger.yml` also fires on `discussion_comment` (created) when a
trusted author (OWNER/MEMBER/COLLABORATOR) writes `/opencode`. The job
passes `DISCUSSION_NUMBER/TITLE/BODY/URL`; `stack_kick.py` switches to
discussion mode when `DISCUSSION_NUMBER` is non-empty: it fetches the
comment thread over REST with the workflow's GITHUB_TOKEN (new
`discussions: write` permission - read for the fetch, write for the
comment-back), budgets it (4000 for the post, 2000 per comment, 12000
total), and kicks a session titled `D#<n>: <title>` whose prompt's
mission is: read the thread, dedupe against existing issues
(`gh issue list --search`), and `gh issue create` one issue per piece of
work with exactly `## Context` / `## Plan` / `## Happy path` /
`## Criteria of success` sections, linking back to the discussion. The
agent must NOT label them `agent-task` - a human reviews first and labels
deliberately (0035 one-shot semantics stay meaningful). Done, it comments
the issue links on the discussion (best effort GraphQL via the stack's
GH_TOKEN). No branch, no PR, no `mask ci` - the deliverable is issues.

No done-guard in discussion mode: a `/opencode` comment cannot linger the
way a label does, so every kick is deliberate; the prompt-level dedupe is
the control against duplicate issues. Guard/fetch API failures degrade to
proceeding, as in 0035.

Workflow feedback comments move from REST issue comments to GraphQL
`addComment(subjectId)` - the one mutation covers issues and discussions
(`${{ github.event.issue.node_id || github.event.discussion.node_id }}`),
so three feedback steps serve both event types instead of six. The
concurrency group gains the event-name prefix because issue and
discussion numbers are independent sequences.

Rejected: labeling discussions (`discussion` `labeled` event) - labels on
discussions are near-invisible UI, and the comment is the natural "tag".
Rejected: the agent answering in the discussion without issues - the
issue is the unit the rest of the loop (labels, kicks, PRs) understands.

## Consequences

- Positive: discussion -> tagged agent -> reviewed issues, with zero new
  long-lived processes and no PAT changes (GITHUB_TOKEN only).
- Positive: issue feedback comments now survive any future subject type
  that implements GraphQL `Commentable`.
- Negative / accepted: the kicked agent holds the stack's GH_TOKEN
  (0030) and reads untrusted thread text (public repo); issue creation
  as the bot is the worst case, and human review gates any `agent-task`
  label. Same posture as issue kicks, one trust level up.
- Accepted: the bot PAT needs `Issues: write` for `gh issue create` and
  `Discussions: write` for the summary comment. Both are unverified
  (TODO(verify) - the PAT's grants live with the operator, and the
  runbook already lists four sibling permissions pending). The prompt
  degrades: if a call is denied the agent names the missing permission
  in its final message and stops; the workflow's own session-link
  comment never depends on the PAT.
- Accepted: thread fidelity caps at the 12000-char budget; a longer
  thread is re-kickable after pruning, and the opening post plus latest
  comments are what fit.

## Links

- Amends the "discussions remain unhandled" note of
  [0033](0033-issue-triggered-agent-kicks.md); trigger semantics of
  [0035](0035-one-shot-labels-and-done-guard.md) (no done-guard here);
  feedback via [0034](0034-session-observability.md); in-stack
  credentials via [0030](0030-opt-in-github-write-credentials-in-stacks.md).
- Issue: <https://github.com/Innoptech/veggies/issues/23>
