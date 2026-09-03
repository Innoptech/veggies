output "governed_repos" {
  description = "Repositories with branch protection applied."
  value       = sort(keys(github_branch_protection.main))
}

output "environment_name" {
  description = "Protected environment created per repo."
  value       = var.environment_name
}

output "label_workflow_branches" {
  description = "Side branches carrying the labeller workflow (merge via PR)."
  value       = sort([for r in github_repository_file.needs_team_review_workflow : r.branch])
}
