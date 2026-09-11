---
status: accepted
date: 2026-09-11
---

# 0053. Repository rulesets and the merge queue

## Context and problem statement

ADR 0007 put merge policy in code as classic `github_branch_protection` with
`required_status_checks { strict = true }` and linear history: every merge to
main invalidates every other open PR. That policy was calibrated for
human-paced PR flow, and this repo no longer has human-paced PR flow - the
agent stack (ADR 0033/0035) opens `agent/issue-N` PRs autonomously, each
one-shot session branches off `origin/main`, and no process owns keeping the
open PRs current. With N parallel PRs the strict rule costs roughly N-squared
check runs plus one manual "Update branch" click per PR per merge, paid by
the operator - the rebase monkey for the automation (discussion #71). ADR
0046's ready gate narrowed the race but accepted a residual
ready-while-behind window "without a merge queue" - this ADR closes it.

The cause is machine-paced PR flow, not human habits. The fix must delete the
update-branch click, not automate it, and must keep the strict guarantee:
main is exactly the tree CI validated (`infra-apply.yml` converges a
production box from it).

## Decision drivers

- The strict/linear-history guarantee is load-bearing; relaxing it is
  rejected on principle (discussion #71).
- Merge queues are not expressible in classic `github_branch_protection`;
  they require repository rulesets - the migration 0007 deferred with a
  TODO(verify) on plan/repo-visibility entitlement.
- Policy stays reviewable tofu, never clicked settings (0007's reason).
- A queue fronting expensive checks only moves the wait - check cost is
  tracked separately (issues #76/#77).
- Every required context must report on `merge_group` runs or the queue
  stalls Pending forever (the always-report invariant,
  terraform/github/README.md).

## Entitlement check (0007's TODO(verify), answered)

- `Innoptech/veggies` is public and org-owned (API-verified 2026-09-11):
  repository rulesets and merge queue are available on all public repos
  regardless of plan. No rulesets existed.
- A real `tofu plan` against the lock-pinned provider (integrations/github
  6.13.0) planned the full ruleset with `merge_queue` clean (`1 to add`). The
  probe caught that 6.13.0 requires `merge_method` uppercase; the probe's
  validity is tied to the locked version (the constraint is `>= 6.0`).
- Final proof is the operator's apply. If the API rejects it anyway, the
  fallback from #71 stands: cheap scoped CI (#76/#77) plus native auto-merge,
  and the queue docs revert with the mechanism.

## Considered options

- GitHub merge queue via `github_repository_ruleset` (chosen)
- Relax `strict = true`
- Kodiak/Mergify-style auto-update bots
- Home-grown auto-rebase / re-kick machinery

## Decision outcome

`terraform/github/` migrates per repo from `github_branch_protection` to
`github_repository_ruleset` ("main branch policy"), carrying every 0007
guarantee verbatim: no bypass actors (= `enforce_admins`), linear history, no
force-push or deletion, code-owner review with stale dismissal and
conversation resolution, identical required check contexts, strict up-to-date
semantics. The ruleset adds an opt-in merge queue (`merge_queue_repos`,
default empty): a repo opts in only after its required-check workflows
trigger on `merge_group`, or every queued merge stalls Pending forever.

Queue shape: `merge_method = REBASE` (house practice: main has no merge
commits and the repo allows squash+rebase only - API-verified; if apply
rejects it the fallback is SQUASH, which would collapse multi-commit agent
PRs), `grouping_strategy = ALLGREEN`, groups of up to 5, minimum 1 (a solo PR
never waits for a group), `check_response_timeout_minutes = 60` (GitHub
default; TODO(verify): size it from the first real queued run - the full
suite includes the 7-role molecule matrix and pytest on the self-hosted
runner pool that kicked sessions also occupy. If timeout evictions appear,
raise the timeout before shrinking the batch).

`.github/workflows/infra-ci.yml` triggers on `merge_group`; merge groups run
the full suite - the group is the exact tree entering main, so path-gating
stays a pull_request-only optimization and must never be "optimized" onto the
merge-group path (whichever of #77/this ADR's PR lands second owns
reconciling the changes-job `all` step and the wiring test).

Migration is a two-phase apply (operator-run): first
`tofu apply -target=module.github.github_repository_ruleset.main` (the
ruleset stacks on top of classic protection - union enforced), then the full
apply removes the classic resource. No `moved` blocks exist across resource
types; the phases keep a zero-gap protection window.

## Consequences

- Positive: the update-branch ritual is deleted; the queue re-verifies the
  combined tree once per group and merges serially; a failing PR is evicted
  without disturbing the line; the strict guarantee is kept by construction;
  ADR 0046's ready-while-behind residual closes.
- Negative / accepted: eviction is the operator's new recurring loop - an
  evicted agent PR is re-driven by a `/opencode` comment or a fresh
  `agent-task` label, so flaky CI now converts directly into kick spend (ADR
  0051/0052 ledger); evictions are CI-health signals. Merge groups run the
  full suite forever: until #76 lands that is seven fedora44-systemd image
  builds per group - the price of killing the rebase storm now; runner
  capacity (one VPS, ADR 0005) becomes the explicit throughput ceiling. The
  merge button becomes the queue on opted-in repos, for human and agent PRs
  alike (agent-kick delivery PRs, labeller PRs).
- Latent: `environments.tf`'s `deployment_branch_policy { protected_branches
  = true }` keys off "protected branches"; post-migration that set is
  ruleset-defined. TODO(verify) before `infra-apply.yml` is ever re-enabled
  (it is disabled under ADR 0024): confirm GitHub counts ruleset-protected
  refs for environment deployment policies.

## Pros and cons of the options

### Merge queue via repository rulesets

- Good: deletes the click rather than automating it; native and boring;
  keeps strict by construction; rulesets are the richer policy primitive
  0007 already deferred to.
- Bad: requires the ruleset migration and the `merge_group` CI trigger;
  per-repo opt-in is one more module variable.

### Relax strict / auto-update bots / home-grown auto-rebase

- All rejected (discussion #71): relaxing strict surrenders "main is exactly
  the tree CI validated"; the bots automate the wasteful click and still burn
  the full matrix per update; home-grown machinery reimplements the queue
  badly and races the one-shot label / done-guard semantics.

## Links

- [Discussion #71](https://github.com/Innoptech/veggies/discussions/71), issue #79
- ADR 0007 (policy as code - the deferral this answers), ADR 0024 (review
  override on this repo), ADR 0046 (ready gate; residual window closed here),
  ADR 0005 (runner capacity)
- [terraform/github/](../../terraform/github/README.md)
