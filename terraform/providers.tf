# The github provider authenticates as the veggies-harness GitHub App
# (ADR 0063): `mask tofu-plan` / `mask tofu-apply` export the vault's
# github_app_* keys as TF_VAR_github_app_* (scripts/tfvars_from_vault.py).
# Never put credentials in tfvars. GITHUB_TOKEN must NOT be set in the
# environment - a token silently wins over app_auth (the mask tasks unset it).
# `owner` is mandatory under App auth (403 "Resource not accessible by
# integration" without it).
provider "github" {
  owner = var.github_owner
  app_auth {
    id              = var.github_app_id
    installation_id = var.github_app_installation_id
    pem_file        = var.github_app_private_key
  }
}

# OpenStack provider: intentionally UNWIRED. An unconfigured openstack
# provider fails `tofu plan` even when the ovh module is count=0 (providers
# configure unconditionally). The scaffold module stays in ovh/; wire both
# back at the ADR 0002 migration:
# provider "openstack" {
#   cloud = var.openstack_cloud
# }
