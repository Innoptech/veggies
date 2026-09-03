# terraform/github - GitHub policy as code (ADR 0007)

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
| `github_actions_secret` / `github_actions_variable` | per-repo Actions secrets/variables from the vault |
| `github_actions_runner_group` | optional org-level runner group (orgs only) |

## Variables

See `variables.tf` - every variable has a description and a type. The ones you
must set: `repos`, `admin_login`, `required_checks` (must match the check
names the project repos' CI actually reports).

## Be careful

- `required_checks` entries must be contexts that **always** report. A
  path-filtered workflow that doesn't run leaves a required check pending
  forever and blocks every merge. This is why the labeller is not a required
  check.
- Actions secret values are stored in the (local, gitignored, backed-up) tofu
  state. See ADR 0008 for the state story.
- Each project repo needs a `CODEOWNERS` file naming the human, or
  `require_code_owner_reviews` has nothing to bind to.
