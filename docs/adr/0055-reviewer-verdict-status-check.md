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
   `merge_group`, checks out the DEFAULT BRANCH on every event and
   creates the `pr-review-agent` check run via the Checks API
   (`scripts/pr_review_gate.py`). The gate script must never execute
   PR-authored code: on the PR events `GITHUB_SHA` resolves to a
   PR-controlled merge ref, and `base.sha` is the tip of whatever branch
   the PR targets - attacker-controlled for PRs to unprotected branches
   (verified: the ruleset covers main only) - so the default branch is
   the only trusted ref. The reviewer session never writes the check.
   Enforcement, scoped honestly: check runs require an App token - the
   classic PAT every `github: true` session holds (0030) cannot mint one
   - and terraform pins `integration_id` 15368 (github-actions; verified
   via `gh api apps/github-actions`), so ACROSS INTEGRATIONS the context
   is pinned to the GitHub Actions App: no classic PAT and no other
   app's status satisfies it. WITHIN the repo, any workflow holding
   `checks: write` could still repaint the context - inside the Actions
   app the single writer remains convention, backstopped by the pytest
   guard that pins the gate workflow's job name: `gate`, never
   `pr-review-agent`, because Actions auto-creates a per-job check run
   and a name match would satisfy the required context on mere job
   completion.
2. **The verdict contract.** The verdict is one machine-readable line in
   the reviewer's review body: `pr-review-verdict: pass` /
   `pr-review-verdict: fail`. Trusted from OWNER/MEMBER/COLLABORATOR
   reviews only (the repo is public - anyone can submit a review), pinned
   to the current head sha, latest wins by `(submitted_at, review id)`
   (the id breaks same-second ties; higher id = newer); `edited` and
   `dismissed` re-trigger the workflow, and a DISMISSED review stops
   counting as a verdict. A review carrying the marker is never a
   clearing act either - a verdict must not clear itself. This contract
   is this PR's side of record; #102 conforms to it, and must re-review
   on `synchronize` of ready PRs - otherwise fixes posted after a fail
   wedge on the human lane.
3. **The state machine** (`scripts/pr_review_gate.py`; pure functions,
   pytest-pinned). Draft short-circuits to success ("the gate evaluates
   at the ready transition") - this is what keeps 0046's ready-gate
   deadlock-free. Then BOTH red lanes are evaluated on every call: a
   declared-scope diff with no human act on this head is red (decision
   4), and a fail verdict with no human act at or after the verdict's
   `submitted_at` is red - one red never hides behind another lane's
   clearing act, and a red summary covers every red lane (the title
   names the first). Otherwise: a cleared lane (scope diff or fail
   verdict with a qualifying act) -> success, titled "human override"
   and naming what was cleared and by whom; verdict pass -> success; no
   verdict -> pending (a human act alone never substitutes for the
   verdict); `merge_group` -> success on the group head ("gated at PR
   head; the group run verifies CI only") - 0053's always-report
   invariant. Renames count on BOTH names (`previous_filename` and
   `filename`): a rename out of scope still hits the sentinel. And fail
   closed is a NAMED red check, never an absent context: any evaluation
   error after the head sha is known reports `pr-review-agent: gate
   error` (the summary names the exception class only - messages can
   carry untrusted API text) and the job exits 1. A >3000-file diff
   (pagination overflow) reports that same red unless a sha-bound
   `/gate-override` names this exact head - the explicit human act is
   the escape for an unscannable diff.
4. **Declared-scope roots - the hard-fail set**, computed in CI from the
   PR file list, independent of the model by construction: `secrets/`,
   `.github/workflows/`, `terraform/`, `agent-config/`, `scripts/`,
   `AGENTS.md`, `CLAUDE.md`, `veggies.yml`, any `CODEOWNERS`
   (basename-matched), `cli/permission_envelope.py`,
   `ansible/roles/egress/`. Deliberately not the labeller's
   `label_paths`: that is a data-plane sensitivity list for a label; this
   is the control-plane trust surface an agent must never clear itself.
   (The labeller is disabled on this repo anyway.)
5. **Human lanes - the merge stays human.** A red check clears only on a
   human act bound to the failing signal: an APPROVED review pinned to
   the current head's `commit_id` by an OWNER/MEMBER (non-self, and
   never a marker-carrying review - decision 2), or an OWNER/MEMBER PR
   comment of the exact form `/gate-override <full-head-sha>`. The
   override is head-bound by construction - the human names the sha they
   read - which REPLACES timestamp postdating against the head's
   committer date: that signal was forgeable via `GIT_COMMITTER_DATE`
   and is gone. For scope-red, head-binding IS the postdating, so an
   override posted while the PR was a draft clears the post-ready red at
   that same head - accepted, and now explicit. Timestamp postdating
   survives only against a fail verdict: the act's time must be >= the
   verdict's `submitted_at`. Both lanes are agent-forgeable under 0043's
   shared identity (sha-binding stops stale-head laundering, not
   impersonation); accepted exactly as 0043 accepted its own trade,
   sunset at #56. Honest consequence: on this checks-only repo
   ([0024](0024-interim-access-and-identity-constraints.md)) the gate is
   advisory until #56; on adopted repos with strict code-owner review
   the ruleset stays the real control regardless.
6. **Terraform opt-in.** `pr_review_gate_repos` (default `[]`,
   subset-validated against `repos`) mirrors `merge_queue_repos`.
   `require_code_owner_review`, CODEOWNERS and the bot identity are
   untouched (0007). Activation is a human step once #102 is live: one
   tfvars line + `tofu apply`. Revocation = delete the line - and
   un-require BEFORE the workflow leaves a repo's default branch, or the
   context pends forever. Kill switch without an apply: repo Actions
   variable `PR_REVIEW_GATE=disabled` - the gate reports success
   immediately and skips all reads.
7. **The verdict log - harvested, never written by the workflow.** The
   GitHub review history is the system of record. The gate workflow does
   NOT write the log: the self-hosted runner container is ephemeral and
   mounts only its `_work` dir (verified against
   `ansible/roles/github_runner/templates/gh-runner@.container.j2`), so
   a workflow-side append would vanish at job end, silently. Instead
   `scripts/pr_review_verdicts.py` is a pull-based harvester run by the
   operator (runbook section 11): it scans recently updated PRs and
   appends one record per non-DISMISSED trusted verdict review to
   `pr-review-verdicts.jsonl` next to `spend.jsonl` (0051) - idempotent
   by `review_id`, fail-open on write errors, tolerant of a missing or
   partly malformed log. No automation yet (a systemd timer is deferred
   substrate work), and no data can be lost: GitHub is authoritative and
   the harvester backfills. The record schema is pinned and
   pytest-enforced, 12 keys: `ts`, `repo`, `pr`, `head_sha`, `event`,
   `state`, `reasons`, `verdict`, `verdict_review_url`, `human_actor`,
   `resolution`, `review_id`. Records carry `event: "harvest"`,
   `state: "verdict"`, `ts` = the review's `submitted_at`, and
   `resolution: "human-override"` when a human act on that head
   postdates the verdict - this log is the only evidence base decision
   9's trigger will ever have.
8. **No auto-merge keys off the agent verdict.** Stated plainly:
   merge-queue auto-merge after human approval + green checks (0053) is
   not verdict-keyed auto-merge. The gate orders human attention; it
   never merges. The `merge_group` arm's assumption, stated: queue entry
   requires the PR's required checks green at its own head -
   `pr-review-agent` included on opted-in repos - so the group run
   re-reports a fixed green rather than re-evaluating.
9. **Re-entry trigger for delegated merge: evidence conditions, never a
   countdown.** The GitHub App identity (#56) is a HARD PRECONDITION - a
   delegated merge with a forgeable approval lane is not a risk you can
   price - plus a logged ~quarter of near-zero human disagreement with
   the reviewer, measured from the harvested verdict log: the share of
   `verdict: "fail"` records carrying `resolution: "human-override"`
   (runbook section 11 has the query); then a new ADR honestly pricing
   the second identity, the CODEOWNERS change, and a threat-model
   section. Until then no issue or PR grants an agent merge rights.

## Consequences

- Positive: single writer, enforced across integrations; the model
  cannot talk its way out of the scope sentinel; human attention becomes
  the explicit final step of every agent PR; revocation is one tfvars
  line; the verdict log pre-pays the re-entry decision.
- Negative / accepted: the gate is advisory on this repo until #56
  (0024's checks-only interim); the verdict marker is forgeable under
  0043's shared identity, bounded by the human lane being the only merge
  event; WITHIN the repo the single-writer rule is convention (any
  `checks: write` workflow could repaint the context - decision 1);
  `/gate-override` comments are unforgeable-by-construction only
  post-#56 for the identity layer - the sha-binding already stops
  stale-head laundering, not impersonation; the verdict log lags until
  the operator runs the harvester (a systemd timer is deferred substrate
  work); check-run summaries embed changed paths verbatim (display-only;
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
