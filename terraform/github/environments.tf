# Protected environment per repo: infra-apply (and anything equally sensitive)
# cannot run without the human's approval. ADR 0007.

data "github_user" "admin" {
  count    = var.admin_login != "" ? 1 : 0
  username = var.admin_login
}

resource "github_repository_environment" "production_infra" {
  for_each          = toset(var.repos)
  repository        = each.key
  environment       = var.environment_name
  can_admins_bypass = false # the human gate applies to admins too

  dynamic "reviewers" {
    for_each = var.admin_login != "" ? [1] : []
    content {
      users = [data.github_user.admin[0].id]
    }
  }

  deployment_branch_policy {
    protected_branches     = true
    custom_branch_policies = false
  }
}
