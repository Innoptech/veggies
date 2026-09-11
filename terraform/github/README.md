# terraform/github - GitHub policy as code

Makes the merge policy of every governed repo reviewable code instead of
clicked settings. The core guarantee: **an agent can open PRs but can never
merge its own work** - every merge needs a green required check and an
approving review from a CODEOWNER (the human; the bot is never a code owner).

## What it manages

| Resource | Effect |
|----------|--------|
| `github_branch_protection.main` | PRs required, 1 approving **code-owner** review, linear history, no force push/delete, strict required checks, conversation resolution, `enforce_admins` (admins not exempt) |
| `github_repository_environment.production_infra` | `production-infra` environment gated on the human (`can_admins_bypass = false`) |
| `github_issue_label.needs_team_review` | the label |
| `github_repository_file.needs_team_review_workflow` | labeller workflow on branch `infra/needs-team-review` (opt-in via `manage_label_workflow`) |
| `module.agent_kick_*` | per-repo agent-kick install: workflow + kick script on `infra/agent-trigger`, `agent-task` label, `VEGGIES_*` secret + variables (ADR 0044) |
| `github_actions_secret` / `github_actions_variable` | per-repo Actions secrets/variables from the vault |
| `github_actions_runner_group` | optional org-level runner group (orgs only) |

## Variables

See `variables.tf` - every variable has a description and a type. The ones you
must set: `repos`, `admin_login`, `required_checks` (must match the check
names the project repos' CI actually reports).

## Install agent kicks on a repo (ADR 0044)

Same register as the rest of this module: reviewable code instead of
clicked settings. A repo opts into the agent in one reviewed block in
`agent_kicks.tf`; removing the block opts it out.

```hcl
module "agent_kick_data_pipelines" {
  source = "./modules/agent-kick"

  repo           = "data-pipelines"
  stack_port     = 8123 # from `veggies ls`
  stack_password = var.veggies_stack_password

  workflow_content    = file("${path.module}/../../.github/workflows/agent-trigger.yml")
  kick_script_content = file("${path.module}/../../scripts/stack_kick.py")
}
```

The recipe: the repo's stack serving + the tofu identity holding Actions
`Secrets: write` / `Variables: write` / `Contents: write` ON THE NEW REPO
(TODO(verify): pushing the workflow file may need more per token type -
classic PAT: the `workflow` scope; GitHub App: the Workflows repository
permission; fine-grained PAT behavior unverified - the first apply
surfaces it loudly) -> the block plus a line per block in the
`agent_kick_delivery_branches` output map in `outputs.tf` ->
`mask tofu-plan` / `mask tofu-apply` -> merge the delivered
`infra/agent-trigger` PR (and delete the delivery branch) -> label an
issue `agent-task`. The serve password lands in the local tofu state like
the other Actions secrets. Full recipe and the veggies-repo migration
note: [the runbook](../../docs/runbook.md#install-agent-kicks-on-a-repo-adr-0044).

## Be careful

- `required_checks` entries must be contexts that **always** report. A
  path-filtered workflow that doesn't run leaves a required check pending
  forever and blocks every merge. This is why the labeller is not a required
  check.
- Actions secret values are stored in the (local, gitignored, backed-up) tofu
  state. State is local; see ../backend.tf for the story.
- Each project repo needs a `CODEOWNERS` file naming the human, or
  `require_code_owner_reviews` has nothing to bind to.
