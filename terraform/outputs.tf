output "governed_repos" {
  description = "Repositories with branch protection applied."
  value       = module.github.governed_repos
}

output "environment_name" {
  description = "Protected environment created per repo."
  value       = module.github.environment_name
}

output "label_workflow_branches" {
  description = "Side branches carrying the labeller workflow (merge via PR)."
  value       = module.github.label_workflow_branches
}
