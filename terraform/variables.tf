variable "github_owner" {
  type        = string
  description = "GitHub org or user that owns the project repositories."
  # TODO(you): set in terraform.tfvars (see terraform.tfvars.example)
  default = ""
}

variable "github_repos" {
  type        = list(string)
  description = "Project repositories the runner and branch policy apply to."
  default     = [] # TODO(you)
}

variable "admin_login" {
  type        = string
  description = "GitHub login of the human reviewer (required reviewer on protected environments)."
  default     = "" # TODO(you)
}

variable "required_checks" {
  type        = list(string)
  description = "Status check contexts required before merge on every governed repo. Must match real CI check names."
  default     = ["pre-commit"] # TODO(you): match the project repos' workflows
}

variable "actions_secrets" {
  type        = map(map(string))
  description = "Actions secrets per repo: { repo = { NAME = value } }. Fed from the ansible vault, never from tfvars."
  sensitive   = true
  default     = {}
}

variable "actions_variables" {
  type        = map(map(string))
  description = "Plain Actions variables per repo: { repo = { NAME = value } }."
  default     = {}
}

variable "environment_name" {
  type        = string
  description = "Protected environment name used by infra-apply and sensitive workflows."
  default     = "production-infra"
}

variable "runner_group_name" {
  type        = string
  description = "Org-level runner group restricted to governed repos. Empty disables (orgs only)."
  default     = ""
}

variable "label_paths" {
  type        = list(string)
  description = "Path globs that trigger the needs-team-review label workflow."
  default     = ["**/sql/**", "**/migrations/**", "**/pipelines/**", "**/models/**", "docs/adr/**"]
}

variable "manage_label_workflow" {
  type        = bool
  description = "Commit the labeller workflow to a side branch per repo (merge via PR by hand)."
  default     = false
}

# enable_ovh returns in phase 3 together with the ovh/ module that consumes it.
# Declaring unused variables trips tflint's terraform_unused_declarations.

variable "openstack_cloud" {
  type        = string
  description = "clouds.yaml entry name for the OpenStack provider (scaffold only, phase 3)."
  default     = null
}
