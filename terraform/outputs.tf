output "governed_repos" {
  description = "Repositories with the default-branch ruleset applied."
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

output "agent_kick_delivery_branches" {
  description = "Per agent-kick block: the delivery branch whose PR installs the workflow files (merge by hand)."
  value       = module.github.agent_kick_delivery_branches
}
