---
status: accepted
date: 2026-09-12
---

# 0054. Comment helper joins the agent-kick delivery set

## Context and problem statement

Issue #101. Workflow run 34706580396 (2026-09-12): GitHub executed the
discussion-ack `addDiscussionComment` mutation and then returned HTTP 200
+ errors[]; the comment landed AND the job went red. The fix moved all
four inline jq/curl comment blocks of `agent-trigger.yml` into a tested
stdlib helper, `scripts/gh_comment.py`, which verifies ground truth (the
subject's recent comments) before a single retry. That made the workflow
exec a THIRD repo file at runtime, while 0048's module delivered two -
the next adopted-repo delivery PR would have pushed a workflow calling a
missing script, breaking every comment step there. Alternative considered:
folding the helper into `stack_kick.py` as a comment mode - rejected: a
766-line kick-shaped script with a kick-shaped env contract is not the
home for a single-purpose commenter (rule 4/rule 7), and the module
wiring is small.

## Decision

The agent-kick delivery set is three files:
`.github/workflows/agent-trigger.yml`, `scripts/stack_kick.py`,
`scripts/gh_comment.py`. The module gains `comment_script_content` (same
shape as `kick_script_content`) and a third
`github_repository_file.gh_comment`, serialized after the kick script
(`depends_on`: branch -> workflow -> kick script -> comment script).
Rollout: merge the master PR, then `tofu apply`, then merge each adopted
repo's delivery PR in the same window (a delivery merged before the
master pushes a workflow calling a missing script).

## Consequences

- Amends [0048](0048-agent-kick-install-as-one-module.md)'s "Two
  `github_repository_file` resources" (now three) and its
  branch -> workflow -> kick-script chain (now four links). This repo is
  unaffected (`manage_files = false`; the runner checks out main every
  run).
- Positive: adopted repos receive the false-red fix through the same
  one-module install; the next comment path (0046's deferred PR path)
  inherits `gh_comment.py` instead of a fifth copy-paste.
- Negative / accepted: the delivered set is three files to keep paired -
  the delivery PR's diff remains the clobber guard.
