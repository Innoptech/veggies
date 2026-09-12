---
status: accepted
date: 2026-09-12
---

# 0055. The reviewer verdict gates; it never merges

## Context and problem statement

Discussion #100 converged on build-the-reviewer / decline-merge-authority:
[0007](0007-github-policy-as-code.md) (policy as code; the bot is never a
code owner) and [0030](0030-opt-in-github-write-credentials-in-stacks.md)
(the review gate is the merge control, not the pod) stand. #102 builds the
reviewer itself - a comment-only agent kicked at `ready_for_review`. #103
(this one) expresses its verdict as a required status check,
`pr-review-agent`.

Constraints that shaped the design:

- **#102 was unlanded when this landed.** Contract-first sequencing, the
  [0051](0051-spend-log-record-contract.md) pattern: this PR pins the
  verdict contract; #102 conforms to it.
- **[0046](0046-draft-first-pr-lifecycle.md)'s ready-gate deadlocks
  against a post-ready check.** The ready transition waits for green
  checks (`checks --watch`) before `gh pr ready`; a required check that
  only verdicts post-ready would block readying forever. The gate must
  evaluate green while the PR is still a draft.
- **[0053](0053-repository-rulesets-and-the-merge-queue.md)'s ruleset has
  no bypass actors.** A red required check is absolute: a wrong red wedges
  every merge until a human clears it.
- **[0043](0043-interim-shared-identity-trigger.md)'s interim shared
  identity.** Agent and human post under one login until #56, so any
  human-looking clearing signal is agent-forgeable - priced, not hidden.

## Decision

1. **Single writer, enforced.** One workflow,
   `.github/workflows/pr-review-gate.yml`, triggered on
   `pull_request_target` / `pull_request_review` / `issue_comment` /
   `merge_group`, checks out BASE-branch code (on the PR events
   `GITHUB_SHA` resolves to a PR-controlled ref; the gate script must
   never execute PR-authored code) and creates the `pr-review-agent`
   check run via the Checks API (`scripts/pr_review_gate.py`). The
   reviewer session never writes the check. Enforcement, not convention:
   check runs require an App token - the classic PAT every `github: true`
   session holds (0030) cannot mint one - and terraform pins
   `integration_id` 15368 (github-actions; verified via
   `gh api apps/github-actions`) so no other integration's status
   satisfies the context. The workflow's single job is named `gate`,
   never `pr-review-agent`: Actions auto-creates a per-job check run, and
   a name match would satisfy the required context on mere job
   completion.
2. **The verdict contract.** The verdict is one machine-readable line in
   the reviewer's review body: `pr-review-verdict: pass` /
   `pr-review-verdict: fail`. Trusted from OWNER/MEMBER/COLLABORATOR
   reviews only (the repo is public - anyone can submit a review), pinned
   to the current head sha, latest wins; `edited` and `dismissed`
   re-trigger the workflow. This contract is this PR's side of record;
   #102 conforms to it, and must re-review on `synchronize` of ready PRs -
   otherwise fixes posted after a fail wedge on the human lane.
3. **The state machine** (`scripts/pr_review_gate.py`; pure functions,
   pytest-pinned), evaluated in order:
   - draft -> success ("the gate evaluates at the ready transition") -
     this is what keeps 0046's ready-gate deadlock-free;
   - declared-scope diff -> failure (decision 4);
   - verdict fail -> failure; verdict pass -> success;
   - no verdict -> pending;
   - `merge_group` -> success on the group head ("gated at PR head; the
     group run verifies CI only") - 0053's always-report invariant; queue
     entry already demanded the green gate at each PR's own head.
4. **Declared-scope roots - the hard-fail set**, computed in CI from the
   PR file list, independent of the model by construction: `secrets/`,
   `.github/workflows/`, `terraform/`, `agent-config/`, `scripts/`,
   `AGENTS.md`, `CLAUDE.md`, `veggies.yml`, any `CODEOWNERS`
   (basename-matched), `cli/permission_envelope.py`,
   `ansible/roles/egress/`. Deliberately not the labeller's
   `label_paths`: that is a data-plane sensitivity list for a label; this
   is the control-plane trust surface an agent must never clear itself.
   (The labeller is disabled on this repo anyway.)
5. **Human lanes - the merge stays human.** A red check clears only when
   a human act POSTDATES the failing signal: an APPROVED review on the
   current head by an OWNER/MEMBER (non-self), or an OWNER/MEMBER PR
   comment starting with `/gate-override`. Postdating is measured against
   the fail verdict's `submitted_at`, or against the head committer date
   for scope-red - closing the race where a late fail hides behind a
   stale approval. Both lanes are agent-forgeable under 0043's shared
   identity; accepted exactly as 0043 accepted its own trade, sunset at
   #56. Honest consequence: on this checks-only repo
   ([0024](0024-interim-access-and-identity-constraints.md)) the gate is
   advisory until #56; on adopted repos with strict code-owner review the
   ruleset stays the real control regardless.
6. **Terraform opt-in.** `pr_review_gate_repos` (default `[]`,
   subset-validated against `repos`) mirrors `merge_queue_repos`.
   `require_code_owner_review`, CODEOWNERS and the bot identity are
   untouched (0007). Activation is a human step once #102 is live: one
   tfvars line + `tofu apply`. Revocation = delete the line - and
   un-require BEFORE the workflow leaves a repo's default branch, or the
   context pends forever. Kill switch without an apply: repo Actions
   variable `PR_REVIEW_GATE=disabled` - the gate reports success
   immediately and skips all reads.
7. **The verdict log.** The GitHub review history is the system of
   record; `pr-review-verdicts.jsonl` next to `spend.jsonl` (0051) is the
   queryable rollup, appended fail-open by the gate workflow - logging
   never blocks the check. The workflow can write there because it runs
   on the self-hosted pool; a pod session cannot - only the litellm
   container mounts stack-state
   ([0052](0052-spend-log-writer.md)), and mounting it into opencode
   would hand the workload the spend ledger. The record schema is pinned
   and pytest-enforced, 11 keys: `ts`, `repo`, `pr`, `head_sha`, `event`,
   `state`, `reasons`, `verdict`, `verdict_review_url`, `human_actor`,
   `resolution`. Human overrides are records too
   (`resolution: "human-override"`) - this log is the only evidence base
   decision 9's trigger will ever have.
8. **No auto-merge keys off the agent verdict.** Stated plainly:
   merge-queue auto-merge after human approval + green checks (0053) is
   not verdict-keyed auto-merge. The gate orders human attention; it
   never merges.
9. **Re-entry trigger for delegated merge: evidence conditions, never a
   countdown.** The GitHub App identity (#56) is a HARD PRECONDITION - a
   delegated merge with a forgeable approval lane is not a risk you can
   price - plus a logged ~quarter of near-zero human disagreement with
   the reviewer, measured from the verdict log (fail/scope decisions
   later human-overridden); then a new ADR honestly pricing the second
   identity, the CODEOWNERS change, and a threat-model section. Until
   then no issue or PR grants an agent merge rights.

## Consequences

- Positive: single enforced writer; the model cannot talk its way out of
  the scope sentinel; human attention becomes the explicit final step of
  every agent PR; revocation is one tfvars line; the verdict log pre-pays
  the re-entry decision.
- Negative / accepted: the gate is advisory on this repo until #56
  (0024's checks-only interim); the verdict marker is forgeable under
  0043's shared identity, bounded by the human lane being the only merge
  event; check-run summaries embed changed paths verbatim (display-only;
  GitHub rejects >64KiB summaries with a 422, which fails closed - the
  check simply never reports); two sensitivity lists (`label_paths` vs
  scope roots) can drift; alarm-fatigue risk on a repo where terraform/
  changes are routine - mitigated by summaries that name the exact
  reason, the accepted cost of a sentinel visible on every trust-surface
  PR. Existing open PRs carry no gate context on their heads until a
  re-push; they report once they update.

## Links

- [0007](0007-github-policy-as-code.md) (policy as code - review rules
  untouched), [0024](0024-interim-access-and-identity-constraints.md)
  (checks-only interim on this repo),
  [0028](0028-retire-canvas-own-the-critic-loop.md) (own the critic loop -
  model judgment stays advisory),
  [0030](0030-opt-in-github-write-credentials-in-stacks.md) (the stack
  PAT that cannot mint a check run; the threat model),
  [0040](0040-self-trigger-guard.md) (comment-command discipline -
  `/gate-override` never reaches the kick path),
  [0042](0042-multi-role-plan-review.md) (the roster precedent: model
  opinion as input, never authority),
  [0043](0043-interim-shared-identity-trigger.md) (the shared identity
  both human lanes inherit),
  [0045](0045-repo-declared-verify-gate.md) (the repo-declared verify
  gate the ready path also runs),
  [0046](0046-draft-first-pr-lifecycle.md) (the ready-gate deadlock this
  design avoids), [0048](0048-agent-kick-install-as-one-module.md) (the
  per-repo opt-in pattern),
  [0051](0051-spend-log-record-contract.md) (contract-first sequencing;
  the sibling log),
  [0053](0053-repository-rulesets-and-the-merge-queue.md) (rulesets, no
  bypass actors, the always-report invariant).
- [Discussion #100](https://github.com/Innoptech/veggies/discussions/100);
  issues #102 (the reviewer), #103 (this gate), #56 (the GitHub App
  identity).
