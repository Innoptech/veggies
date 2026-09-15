# terraform/

One root module (this directory) composing child modules:

| Path      | Status        | Contents |
|-----------|---------------|----------|
| `./`      | active        | versions, providers, variables, local-state note |
| `github/` | active        | branch rulesets (merge policy + opt-in merge queue), required checks, environments, Actions secrets, runner group |
| `ovh/`    | **scaffold**  | Public Cloud instance/SG/volume/cloud-init - gated by `var.enable_ovh`, **never applied** until the migration plan is executed (today's host `veggies` is a manually-rented VPS) |

## State

Local by decision: `terraform.tfstate` is gitignored and covered by the
restic backups. `backend.tf` documents the OVH S3-compatible migration target.

## Secrets flow

The github provider authenticates as the `veggies-harness` GitHub App
(ADR 0063): `app_auth` in `providers.tf` reads `var.github_app_id`,
`var.github_app_installation_id` and `var.github_app_private_key`. Those
live ONLY in the ansible vault (`secrets/github.yml`, keys `github_app_*`);
`mask tofu-plan` and `mask tofu-apply` decrypt them to the process
environment as `TF_VAR_*` via `scripts/tfvars_from_vault.py` - plaintext
never touches disk, tfvars, or CI logs. The mask tasks `unset GITHUB_TOKEN`
first: a token in the environment silently wins over `app_auth`. The App's
installation repository list is maintained in the GitHub UI - the
provider's `github_app_installation_repository` resource is documented as
incompatible with App authentication.

## Review commands

```bash
mask tofu-fmt        # formatting, same as CI
mask tofu-validate   # init -backend=false + validate
mask tflint          # bundled terraform ruleset
mask tofu-plan       # read-only; requires secrets + tfvars to be filled
```
