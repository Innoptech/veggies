output "governed_repos" {
  description = "Repositories with the default-branch ruleset applied."
  value       = sort(keys(github_repository_ruleset.main))
}

output "environment_name" {
  description = "Protected environment created per repo."
  value       = var.environment_name
}

output "label_workflow_branches" {
  description = "Side branches carrying the labeller workflow (merge via PR)."
  value       = sort([for r in github_repository_file.needs_team_review_workflow : r.branch])
}

output "agent_kick_delivery_branches" {
  description = "Per agent-kick block: the delivery branch whose PR installs the workflow files (merge by hand). Empty string = the repo holds the masters (manage_files = false). One line per block in agent_kicks.tf."
  value = {
    veggies = module.agent_kick_veggies.files_branch
  }
}
