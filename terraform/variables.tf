variable "github_owner" {
  type        = string
  description = "GitHub org or user that owns the project repositories."
  # TODO(you): set in terraform.tfvars (see terraform.tfvars.example)
  default = ""
}

# Further variables are introduced together with the module that consumes
# them: github/ in phase 2 (e.g. github_repos), ovh/ in phase 3 (enable_ovh).
# Declaring them before they are used trips tflint's terraform_unused_declarations.

variable "openstack_cloud" {
  type        = string
  description = "clouds.yaml entry name for the OpenStack provider (scaffold only, phase 3)."
  default     = null
}
