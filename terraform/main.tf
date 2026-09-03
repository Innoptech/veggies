# Root module. The GitHub policy is live (phase 2); the OVH compute module is a
# gated scaffold that is never applied (phase 3, ADRs 0002/0008).

module "github" {
  source = "./github"

  repos                 = var.github_repos
  admin_login           = var.admin_login
  required_checks       = var.required_checks
  actions_secrets       = var.actions_secrets
  actions_variables     = var.actions_variables
  environment_name      = var.environment_name
  runner_group_name     = var.runner_group_name
  label_paths           = var.label_paths
  manage_label_workflow = var.manage_label_workflow
}

# Phase 3 scaffold - the module directory does not exist yet, so this call is
# commented out deliberately. Phase 3 adds terraform/ovh/ and enables this.
# module "ovh" {
#   source = "./ovh"
#   count  = var.enable_ovh ? 1 : 0
#
#   openstack_cloud = var.openstack_cloud
# }
