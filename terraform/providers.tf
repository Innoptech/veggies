# The github provider authenticates via GITHUB_TOKEN in the environment.
# `mask tofu-plan` / `mask tofu-apply` export it from the ansible-vault
# secrets (scripts/tfvars_from_vault.py). Never put tokens in tfvars.
provider "github" {
  owner = var.github_owner
}

# OpenStack provider: used only by the scaffolded ovh/ module when
# var.enable_ovh = true. Authenticates via clouds.yaml / OS_* env on the
# operator machine. `null` means "fall back to OS_CLOUD".
provider "openstack" {
  cloud = var.openstack_cloud
}
