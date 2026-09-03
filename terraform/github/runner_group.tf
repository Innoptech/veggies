# Optional org-level runner group restricting the self-hosted runners to the
# governed repos. Disabled by default: runner groups require a GitHub
# organization (TODO(verify) account type), and a single-host runner setup
# works without one.

resource "github_actions_runner_group" "agents" {
  count      = var.runner_group_name != "" ? 1 : 0
  name       = var.runner_group_name
  visibility = "selected"
  selected_repository_ids = [
    for repo in data.github_repository.this : repo.repo_id
  ]
}
