# The github provider authenticates via GITHUB_TOKEN in the environment.
# `mask tofu-plan` / `mask tofu-apply` export it from the ansible-vault
# secrets (scripts/tfvars_from_vault.py). Never put tokens in tfvars.
provider "github" {
  owner = var.github_owner
}

# OpenStack provider: intentionally UNWIRED. An unconfigured openstack
# provider fails `tofu plan` even when the ovh module is count=0 (providers
# configure unconditionally). The scaffold module stays in ovh/; wire both
# back at the ADR 0002 migration:
# provider "openstack" {
#   cloud = var.openstack_cloud
# }
