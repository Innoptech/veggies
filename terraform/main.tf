# Root module. The GitHub policy is live (phase 2); the OVH compute module is a
# gated scaffold that is never applied (phase 3, ADRs 0002/0008).

module "github" {
  source = "./github"

  repos                     = var.github_repos
  admin_login               = var.admin_login
  required_checks           = var.required_checks
  required_checks_overrides = var.required_checks_overrides
  actions_secrets           = var.actions_secrets
  actions_variables         = var.actions_variables
  environment_name          = var.environment_name
  runner_group_name         = var.runner_group_name
  label_paths               = var.label_paths
  manage_label_workflow     = var.manage_label_workflow
}

# Scaffold only (ADRs 0002, 0008): enable_ovh stays false while veggies is a
# manually-rented VPS. Enabling is the reviewed migration plan, not an impulse.
module "ovh" {
  source = "./ovh"
  count  = var.enable_ovh ? 1 : 0

  # The openstack provider (var.openstack_cloud) is configured at this level
  # and inherited by the module.
  region         = var.ovh_region
  flavor_name    = var.ovh_flavor_name
  image_name     = var.ovh_image_name
  ssh_public_key = var.ovh_ssh_public_key
  admin_cidr     = var.ovh_admin_cidr
  volume_size_gb = var.ovh_volume_size_gb
}
