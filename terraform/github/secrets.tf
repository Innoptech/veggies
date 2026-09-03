# Actions secrets and variables the workflows in the governed repos need.
# Values arrive via TF_VAR_actions_secrets from the ansible vault
# (mask tofu-plan / tofu-apply) - plaintext never lives in this repo.
#
# NOTE: secret values are stored in the tofu state. State is local, gitignored
# and restic-backed-up (ADR 0008); keep it that way until the S3 backend lands.
# TODO(verify): evaluate the provider's write-only secret attributes to keep
# values out of state entirely.

locals {
  repo_secrets = merge([
    for repo, secrets in var.actions_secrets : {
      for name, value in secrets : "${repo}/${name}" => { repo = repo, name = name, value = value }
    }
  ]...)

  repo_variables = merge([
    for repo, variables in var.actions_variables : {
      for name, value in variables : "${repo}/${name}" => { repo = repo, name = name, value = value }
    }
  ]...)
}

resource "github_actions_secret" "this" {
  for_each        = local.repo_secrets
  repository      = each.value.repo
  secret_name     = each.value.name
  plaintext_value = each.value.value
}

resource "github_actions_variable" "this" {
  for_each      = local.repo_variables
  repository    = each.value.repo
  variable_name = each.value.name
  value         = each.value.value
}
