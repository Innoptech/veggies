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

variable "required_checks_overrides" {
  type        = map(list(string))
  description = "Per-repo required-check contexts, replacing the default list: { repo = [checks] }."
  default     = {}
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

variable "enable_ovh" {
  type        = bool
  description = "Gate for the scaffolded Public Cloud module (ADRs 0002, 0008). Keep false until the migration is planned."
  default     = false
}

variable "openstack_cloud" {
  type        = string
  description = "clouds.yaml entry name for the OpenStack provider (scaffold only)."
  default     = null
}

variable "ovh_region" {
  type        = string
  description = "OVH Public Cloud region for the scaffold (e.g. GRA11, SBG5, BHS5)."
  default     = "" # TODO(you)
}

variable "ovh_flavor_name" {
  type        = string
  description = "Flavor matching 8 vCPU / 16 GB in the chosen region (scaffold only)."
  default     = "" # TODO(you) + TODO(verify): against the region catalog
}

variable "ovh_image_name" {
  type        = string
  description = "Boot image name for the scaffold."
  default     = "Fedora 44" # TODO(verify): exact catalog name in the chosen region
}

variable "ovh_ssh_public_key" {
  type        = string
  description = "Admin SSH public key for the scaffold's cloud-init."
  default     = "" # TODO(you)
}

variable "ovh_admin_cidr" {
  type        = string
  description = "Temporary SSH source /32 for the bootstrap window (scaffold only)."
  default     = "" # TODO(you)
}

variable "ovh_volume_size_gb" {
  type        = number
  description = "Data volume size for the scaffold."
  default     = 200
}
