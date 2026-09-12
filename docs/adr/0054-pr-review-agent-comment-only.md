---
status: accepted
date: 2026-09-12
---

# 0054. Comment-only PR-review agent on the ready transition

## Context and problem statement

ADR [0046](0046-draft-first-pr-lifecycle.md) deferred the first-class PR
path ("PR comments never kick"); issue #102 (from discussion #100)
re-enters that deferral. The operator is the only unqueued,
uninstrumented step in the issue-to-PR pipeline. Every in-session
quality mechanism - the persona plan review (0042), the
adversarial-review subagent, the 0036 supervisor, the 0045 verify gate,
the 0046 ready-gate, CI - runs inside the authoring session against its
own framing, and none of it produces a review object on the PR over the
exact post-rebase commits CI ran on. The thread declined the
merge-authority half: [0007](0007-github-policy-as-code.md)'s invariant
(the bot's approval never satisfies review) stays load-bearing.

## Decision drivers

- Triage, not delegation, is the bottleneck fix: the brief's risk rank
  orders the operator's ready queue.
- Comment-only, never APPROVE/REQUEST_CHANGES: the approval satisfies
  nothing anyway (0007 keeps that true automatically) and an
  approval-shaped artifact invites merge-rights creep.
- The reviewer must run a different model than the author (0028/0036
  invariant: same-model review is myopic).
- Plan-vs-diff, never a cold read: the audit anchors to the issue's
  acceptance criteria and the session's own posted plan (0042).
- The event path has one shape (workflow gate -> stack_kick routing ->
  titled session -> guard family -> observable feedback); this adds a
  third subject type to it without forking anything.
- No new long-lived processes, no inbound ports (0033's posture).

## Decision

1. **Trigger** (`.github/workflows/agent-trigger.yml`):
   `pull_request_target: types: [ready_for_review]`, plus a trusted
   command-anchored `/review` PR comment on the `issue_comment` event.
   NOT plain `pull_request`: under it the workflow *definition* comes
   from the PR head, so an agent-authored PR that edits the workflow
   would execute its own modified copy - with `VEGGIES_STACK_PASSWORD`
   in env - at its own ready transition (verified against GitHub's docs
   2026-09-12: `pull_request_target` runs in the default branch's
   context and "prevents execution of unsafe code from the head of the
   pull request"). The checkout is pinned to the default branch for
   every event, so `scripts/stack_kick.py` and the no-ask scan always
   run trusted code. The auto-kick job-if requires a same-repo head
   (`head.repo.full_name == github.repository` - fork PRs never reach
   the kick) and an `agent/issue-*` head ref (human PRs use `/review`).
   Explicitly NOT `synchronize`: draft-first (0046) pushes early and
   often, so per-push reviews would burn sessions on known-unfinished
   work and flood the 0051 spend log.
2. **Kick mode** (`scripts/stack_kick.py` `main_pr()`, routed on
   `PR_NUMBER`): the review-guard `review_reason()` skips closed/merged
   PRs (the review would audit dead work) and drafts (deliberately
   unfinished); like every guard here it degrades to proceeding on API
   failure. Then the in-flight guard (`PR#N: ` title prefix), the
   no-ask gate, and a session titled `PR#N: <title>` (0034's shape). No
   done-guard: re-readying after rework and re-commenting `/review` are
   deliberate acts (the 0038 discussion stance).
3. **The reviewer is a kicked session plus a new rostered persona**
   (`agent-config/agents/pr-reviewer.md`): `mode: subagent` on
   `litellm/glm-5` - a third lens beside the author's kimi-k3 and the
   in-session adversarial reviewer's deepseek-v4. Its envelope is the
   exact 0041 read-only pattern (`edit`/`bash`/`task`/`webfetch: deny`):
   the session materializes the diff (`gh pr diff`) and a detached
   read-only worktree at the audited head, so the analyst roams with
   read/grep/glob only and structurally cannot execute PR-tree code,
   call gh, or post. Posting stays in the kicked session (0041's
   single-mutation-point rule), whose one write is `gh pr review N
   --comment`. INSPECT, NEVER EXECUTE is a prompt rule for the session
   too: a PR editing `.pre-commit-config.yaml` or a Makefile would
   otherwise turn the audit into code execution with credentials in
   env. The persona's brief contract: the first line is
   `**veggies PR audit** - risk: LOW|MEDIUM|HIGH - audited head <sha>`,
   then Plan vs diff, Threat-model surface (egress, secrets,
   permissions, branch protection, `agent-config/`, the kick prompt,
   the supervisor - including a PR that edits the reviewer's own
   config), Residual
   risks, and a mandatory Not-checked list; ~60 lines hard cap (a brief
   the operator skims is worse than none). The prompt also mandates
   reading prior reviews - a re-review checks flag resolution, never
   regenerates a contradictory second opinion - and a double-post guard
   (a prior bot review stamped with the current head sha means stop),
   so a 0036 supervisor refinement cannot double-post.
4. **Supervision and spend follow the title**: the 0036 critic's
   `KICKED_TITLE` widens to `^(?:PR)?#\d+:` (the review session posts
   public content off untrusted diff material - it is exactly the
   session class that must not escape judging); `cli/costs.py`
   attributes `^PR#(\d+):` to kind `pr`, so `veggies costs` rolls
   review spend up per PR. The split is deliberate: author spend
   (`#M:`) and review spend (`PR#N:`) are separate rows;
   `--session "PR#N"` is the per-review read.
5. **Delivery** (0048 semantics): the masters change here; adopted
   repos pick the reviewer up on the next apply + delivery-PR merge. No
   terraform change - the module reads the masters with `file()`.

Rejected / deferred:

- Plain `pull_request` (decision 1: the PR head would control the
  workflow definition).
- `synchronize` (decision 1).
- The workflow job posting the brief with its GITHUB_TOKEN while the
  session holds no credential (the strongest committee objection): it
  breaks 0033's fire-and-forget kick shape (a synchronous wait plus a
  session-output extraction loop in the workflow - new parallel
  machinery, a runner held for minutes, new failure modes), and its
  gain is capped by pod reality - the bot PAT is ambient pod env (0030)
  regardless of who posts. The true fix (per-purpose installation
  tokens) arrives with the GitHub App identity (#56).
- APPROVE / REQUEST_CHANGES (decision drivers).
- Editing 0046 in place: ADRs are append-only (AGENTS.md rule 5).

## Consequences

- Positive: every ready agent PR gets an independent, different-model,
  risk-ranked audit object over the exact commits CI ran on; the
  operator's review collapses from a cold adversarial read to
  confirming a brief. `/review` extends the audit to human PRs on
  demand.
- Positive: the 0042 plan comment gains a second consumer - the audit
  audits the session against its own posted plan.
- Accepted risk, named (the credential honesty paragraph): GitHub has
  no review-only token scope (posting a review needs
  `pull-requests: write`), and 0030 puts the one bot PAT in the pod
  env of every session - there is no per-session credential channel.
  "Read-only
  credentials" is therefore delivered as a structurally read-only
  *analyst* (no bash, no gh) plus a session whose single write is the
  review call; the boundary for the session itself is prompt-level. The
  residual is bound to #56 (GitHub App per-purpose installation
  tokens). If the letter of the criterion is ever required, the honest
  shape is a separate reviewer stack holding a review-scoped PAT - its
  own issue and ADR.
- Accepted risk, named: different-model is best-effort at the router -
  the litellm fallback chain (`glm-5: [kimi-k3]`) silently degrades the
  reviewer to the author's own model under a glm-5 outage (fail-open is
  the deliberate router posture; adversarial-review's chain shares this
  property). glm-5's upstream id keeps its TODO(verify); pytest pins
  the persona's pin so intent fails loud.
- Accepted, named: `ready_for_review` quietly extends kick authority
  from trusted commenters (0033/0040/0043) to anyone with repo write
  who can push an `agent/issue-*` branch and click ready - today that
  is the operator and the bot PAT.
- Accepted, named: the concurrency group keeps its event-name prefix,
  so `/review` and `ready_for_review` on the same PR do not serialize
  against each other; the `PR#N: ` in-flight guard is the sole
  double-book protection (the 0046-shaped accepted risk).
- Accepted: personas register at stack boot (0019) - `veggies up
  veggie` is required after merge before the reviewer dispatches (same
  posture as 0041).
- Known cosmetic limitation: the reviewer posts under the same bot
  identity as the author until #56 lands - structurally harmless (the
  review is comment-only either way).
- Trajectory note: the workflow's if/env block is accreting a strophe
  per subject type (ISSUE_*, DISCUSSION_*, PR_*) in the most
  injection-sensitive YAML of the repo; when a fifth trigger kind
  lands, pass `GITHUB_EVENT_PATH` through and let stack_kick.py extract
  fields from the payload JSON - the workflow collapses to transport
  and all trigger semantics move into the stdlib-only pytest-covered
  script. NOT done here (scope discipline).

## Links

- Issue #102; discussion #100.
- Re-enters [0046](0046-draft-first-pr-lifecycle.md)'s deferral;
  amends nothing.
- Builds on [0033](0033-issue-triggered-agent-kicks.md) (kick path),
  [0041](0041-discussion-elaboration-persona-roster.md) /
  [0042](0042-multi-role-plan-review.md) (persona roster + plan
  comment), [0043](0043-interim-shared-identity-trigger.md) (interim
  identity), [0048](0048-agent-kick-install-as-one-module.md)
  (delivery), [0051](0051-spend-log-record-contract.md) (spend
  contract), [0036](0036-always-on-critic-for-kicked-sessions.md)
  (supervision).
- [0007](0007-github-policy-as-code.md) stays load-bearing.
