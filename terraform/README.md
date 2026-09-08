# terraform/

One root module (this directory) composing child modules:

| Path      | Status        | Contents |
|-----------|---------------|----------|
| `./`      | active        | versions, providers, variables, local-state note |
| `github/` | active        | branch protection, required checks, environments, Actions secrets, runner group |
| `ovh/`    | **scaffold**  | Public Cloud instance/SG/volume/cloud-init - gated by `var.enable_ovh`, **never applied** until the migration plan is executed (today's host `veggies` is a manually-rented VPS) |

## State

Local by decision: `terraform.tfstate` is gitignored and covered by the
restic backups. `backend.tf` documents the OVH S3-compatible migration target.

## Secrets flow

The github provider needs a token at plan/apply time. Tokens live ONLY in the
ansible vault (`secrets/github.yml`); `mask tofu-plan` and `mask tofu-apply`
decrypt to the process environment via `scripts/tfvars_from_vault.py` -
plaintext never touches disk, tfvars, or CI logs.

## Review commands

```bash
mask tofu-fmt        # formatting, same as CI
mask tofu-validate   # init -backend=false + validate
mask tflint          # bundled terraform ruleset
mask tofu-plan       # read-only; requires secrets + tfvars to be filled
```
