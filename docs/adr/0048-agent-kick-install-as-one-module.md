---
status: accepted
date: 2026-09-11
---

# 0048. Install agent kicks as one terraform module block per repo

## Context and problem statement

[0033](0033-issue-triggered-agent-kicks.md)'s event path works, but
adopting it on another repo was manual: hand-vendor
`.github/workflows/agent-trigger.yml` and `scripts/stack_kick.py` into the
repo, hand-wire the `agent-task` label, and hand-create the
`VEGGIES_STACK_*` Actions secret/variables - the step that 403'd at this
repo's own adoption (0033's adoption note). After that, nothing connects
an adopted repo's copies back to the masters: a master-copy edit here
never reaches them, and the drift is silent. Discussion #42 converged: the
fix for "hard to install" is more terraform, not a new runtime plane. A
GitHub App as the event-delivery mechanism stays deferred (0024
inbound-dark, 0028 retired canvas, 0033 outbound-poll).

## Decision drivers

- The install must be reviewable as a PR diff. terraform.tfvars is
  gitignored, so per-repo wiring must be committed HCL.
- No new long-lived processes, no inbound ports - 0033's posture stands.
- Reuse the labeller's proven side-branch delivery precedent (0007's
  module, `infra/needs-team-review`) instead of inventing a second
  pattern.
- The master copies of the workflow and kick script stay hand-edited and
  dogfooded in this repo; tofu must not own files that PRs edit here.

## Decision

New child module `terraform/github/modules/agent-kick/`; one literal
`module` block per adopted repo in `terraform/github/agent_kicks.tf` - the
roster of repos the agent serves. One block declares everything the kick
path needs on the GitHub side:

- A `github_branch.delivery` resource for the `infra/agent-trigger`
  delivery branch: `github_repository_file` does not create branches
  (verified against provider v6.13.0; `autocreate_branch` is deprecated in
  v6). `github_branch`'s create swallows 422 already-exists and its read
  drops state on 404, so a branch deleted after its PR merged is recreated
  from the default-branch tip on the next delivery.
- Two `github_repository_file` resources:
  `.github/workflows/agent-trigger.yml` and `scripts/stack_kick.py`,
  delivered onto the repo's `infra/agent-trigger` branch (the labeller
  precedent - direct pushes to the protected default branch are rejected).
  `overwrite_on_create = true`: post-merge a fresh delivery branch inherits
  the merged file from the default-branch tip, and overwriting it on the
  DELIVERY branch is the update path (`false` would hard-fail delivery #2).
  The delivery PR's diff is the clobber guard, since nothing reaches the
  protected default branch without a human merge; a repo's hand-vendored
  copy surfaces as that PR's diff instead of an apply error. The chain
  serializes via `depends_on`: branch -> workflow file -> kick script.
- The `agent-task` issue label. It leaves the every-governed-repo loop in
  repos.tf and becomes per-block.
- The `VEGGIES_STACK_PASSWORD` Actions secret, fed by a sensitive variable
  threaded root -> github -> child from the existing vault key
  `veggies_stack_password` (exported as `TF_VAR_veggies_stack_password` by
  scripts/tfvars_from_vault.py).
- The `VEGGIES_STACK_HOST` / `VEGGIES_STACK_PORT` Actions variables.

The masters are this repo's own files, read with `file()` - never
`templatefile()`: the workflow is dense with `${{ }}`, which templatefile
would interpolate. This repo's block sets `manage_files = false` (it holds
the masters); its live label/secret/variables migrate into the module via
4 `moved` blocks in `terraform/github/moves.tf`.

## Consequences

- Positive: install = one block + one apply + one merge, all reviewable.
  A master-copy edit becomes visible per adopted repo in `tofu plan` -
  silent drift becomes visible drift. Uninstall = remove the block +
  apply + a PR deleting the workflow file from the repo's default branch.
- Accepted semantics change: for ADOPTED repos the event path becomes
  tofu-apply snapshots - upgrades propagate by apply + merging the
  delivery PR per repo, NOT by merge to veggies' main. The master repo's
  event path still self-syncs on merge (the runner checks the repo out
  every run); the runbook sync table distinguishes both.
- Accepted: the `agent-task` label's scope narrows to kick-enabled repos;
  the first apply destroys the label on any governed repo without a block.
  Veggies is the only kick consumer at decision time, so nothing live
  changes. Rule: migrate every kick-enabled repo in the same apply.
- Accepted: the delivery branch is deleted after each merge; tofu
  recreates it from the default-branch tip on the next delivery - squash
  merges would otherwise poison the next delivery's diff.
- Migration precondition (operator, before the first plan/apply with
  moves.tf): set `stack_port` in `module.agent_kick_veggies` from
  `veggies ls` - the placeholder 0 would otherwise publish in place over
  the moved variable. The guards are resource-level lifecycle
  preconditions on the port variable and password secret resources:
  validate-safe and plan-loud - variable validations would fail
  `tofu validate` on the placeholder (verified on OpenTofu 1.12.6), which
  would break CI. Also delete
  `actions_secrets.veggies.VEGGIES_STACK_PASSWORD`,
  `actions_variables.veggies.VEGGIES_STACK_HOST` and
  `actions_variables.veggies.VEGGIES_STACK_PORT` from `secrets/github.yml`.
  Delete moves.tf after the first green apply.
- Accepted: the delivered master is not yet generic - it assumes this
  runner fleet (`runs-on: [self-hosted, linux, x64, veggies]`) and
  references the `veggie` stack name in the issue-comment bodies it
  posts. Third-party adoption is gated on parameterizing the workflow
  content; out of scope here.

## Links

- Builds on: [0007](0007-github-policy-as-code.md) (its labeller is the
  side-branch delivery precedent),
  [0033](0033-issue-triggered-agent-kicks.md) - amends its adoption story;
  0033's adoption-time 403 becomes this module's documented per-repo
  permission precondition (Actions `Secrets: write` + `Variables: write` +
  `Contents: write` on the adopted repo; pushing the workflow file may
  need a token-type-specific grant - runbook).
