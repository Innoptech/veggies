---
status: accepted
date: 2026-09-11
---

# 0046. Draft-first PR lifecycle and the redefined done-guard

## Context and problem statement

Until now the PR existed only at the end: the kick prompt told the session
to push and `gh pr create` as the last step, so a session's work was
invisible - and unreviewable - until it declared itself done. When the PR
finally appeared it could be marked ready while unmergeable: this repo's
branch protection (tofu-managed in
[terraform/github/repos.tf](../../terraform/github/repos.tf)) requires a
linear history and up-to-date branches (`required_linear_history = true`,
`strict = true`, `dismiss_stale_reviews = true`), so "ready" said
nothing about merging against current main.

Draft-first was the obvious fix, and
[0035](0035-one-shot-labels-and-done-guard.md)'s done-guard silently
forbade it: the guard blocks a re-kick when an `agent/issue-N` PR exists
*in any state*, so the first draft opened early would make every later
kick of the still-unfinished issue skip as "done". The three therefore
ship together (discussion #39): the draft-first prompt, the ready-gate
that defines "done", and a done-guard whose states match both.

## Decision drivers

- The PR is the record (0035): it should exist from the first commit so
  the diff, the commits, and the CI runs are watchable while work happens.
- "Ready" must mean what the merge button needs: green checks AND
  mergeable against current main, as branch protection defines it.
- The done-guard must track *handled*, not *a PR object exists* -
  draft-first makes an open draft the normal in-flight state.
- Every signal the gate polls must be computable for a draft (see the
  rejected `mergeStateStatus` gate below).

## Decision

1. **Draft-first kick prompt** (`scripts/stack_kick.py`
   `PROMPT_TEMPLATE`): the PR exists from the FIRST commit, not at the
   end. Right after the plan comment lands, push the branch and
   `gh pr create --draft` with "Closes #N" in the body; a re-kick that
   finds an existing PR for `agent/issue-N` reconciles the branch and
   keeps pushing to it, never opens a second. Push early and often: the
   draft's CI runs on every push and is the feedback loop for the checks
   the kick environment cannot run (molecule, the docker pre-commit hook).
   The workflow's kick-comment text (0034) now says exactly this.
2. **Ready is the last act**, behind a gate. Verify first -
   `SKIP=actionlint-docker mask ci` green, claiming only what actually ran
   (the PR's own CI runs the docker hook and molecule) - then:
   a. `git fetch origin && git rebase origin/main` inside the session's
      own worktree - a no-op when main never moved. After any rebase:
      re-run the verify gate (a rebase invalidates the green earned
      pre-rebase) and push with `--force-with-lease`. If the force-push
      rewrites a branch a human already approved, comment on the PR that
      the rebase dismissed the review (`dismiss_stale_reviews` does it
      silently) and that re-approval is needed.
   b. `gh pr checks --watch` - the draft's CI on the final head must be
      green; fix and re-push on red, never mark ready on pending.
   c. `gh pr view --json mergeable` must read MERGEABLE. UNKNOWN is
      transient (GitHub computes mergeability asynchronously) - wait a few
      seconds and re-read, NEVER rebase on UNKNOWN; CONFLICTING means back
      to (a).
   d. Only now `gh pr ready` - the final act; the done-guard treats the
      issue as handled from this moment.
   Work that lands after ready (an ADR 0036 supervisor refinement, a
   review comment) converts back first: `gh pr ready --undo`, rework,
   re-run the whole gate - never push new commits onto a ready PR.
3. **Done-guard redefined** (`done_reason()`): handled = closed issue, or
   merged PR, or open non-draft PR. Open drafts and closed-unmerged PRs
   never block - an open draft is the session's workbench, a
   closed-unmerged PR an abandoned attempt the re-kick reconciles.
4. **PR comments never kick**: the `issue_comment` trigger gains
   `!github.event.issue.pull_request`. The first-class PR path stays
   deferred.

Rejected / deferred:

- A PR-comment `/rebase` listener: `issue_comment` fires on PRs with the
  PR's title/body in the issue slot - the command would mis-kick.
  Recovery stays "human comments `/opencode` on the issue".
- Editing 0035/0034 in place: ADRs are append-only history (AGENTS.md
  rule 5).
- Gating on `mergeStateStatus`: it reads DRAFT until the PR is marked
  ready - the field cannot answer for a draft. The fetch+rebase-first
  design covers behind-main without it.

## Consequences

- Positive: the operator watches commits and CI land on the draft as work
  happens. The draft PR moves session observability beyond the operator's
  box: the repo's PR list becomes the public watch path - no stack
  password, no tunnel needed - for the work
  [0034](0034-session-observability.md) instrumented.
- Positive: a ready PR is green and mergeable against current main; the
  merge button works when the human presses it. Honesty clause: the gate's
  post-watch freshness check (`git merge-base --is-ancestor origin/main
  HEAD` after the CI watch) only narrows the ready-while-behind race to
  the length of one fetch - without a merge queue, that residual window
  is accepted.
- Positive: re-kicking an open-draft issue continues the existing branch
  instead of duplicating it; ready and merged stay blocked.
- Accepted risk, named: the in-flight guard
  ([0040](0040-self-trigger-guard.md)) is now the SOLE double-book
  protection for the whole session, and it degrades to proceeding on
  stack-API failure. Worst case: two sessions reconciling one branch,
  bounded by the worktree tripwire (`-B` refusing "already used by
  worktree") and the `-2` fallback (0037).
- Accepted risk, named: PRs opened from a `-2` recovery branch are
  invisible to the `head=owner:agent/issue-N` done-guard lookup -
  pre-existing (0037), draft-first makes it routine.

## Links

- Issue: #53; discussion #39.
- Amends [0035](0035-one-shot-labels-and-done-guard.md) (done-guard
  states) and [0034](0034-session-observability.md) (the kick-comment
  text).
- Builds on [0037](0037-per-session-worktrees.md) - the rebase runs in
  the session's own worktree.
- Leaves [0040](0040-self-trigger-guard.md) untouched.
