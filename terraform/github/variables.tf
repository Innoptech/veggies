variable "repos" {
  type        = list(string)
  description = "Project repositories to govern (names only; owner comes from the provider config)."
  # TODO(you): set via the root module (terraform.tfvars)

  validation {
    condition     = alltrue([for r in var.repos : can(regex("^[A-Za-z0-9_.-]+$", r))])
    error_message = "Repository names may only contain letters, digits, '-', '_' and '.'."
  }
}

variable "default_branch" {
  type        = string
  description = "Branch that gets the protection rule."
  default     = "main"
}

variable "required_checks" {
  type        = list(string)
  description = "Status check contexts required before merge, applied to every repo."
  # TODO(you): check names must match the workflows in each project repo.
  # Do NOT add the needs-team-review labeller here: path-filtered workflows
  # never report a check on untouched-path PRs, which would block merges.
  default = ["pre-commit"]
}

variable "required_checks_overrides" {
  type        = map(list(string))
  description = "Per-repo required-check contexts, replacing the default list: { repo = [checks] }."
  default     = {}
}

variable "admin_login" {
  type        = string
  description = "GitHub login of the human reviewer (you). Required reviewer on environments; must own CODEOWNERS entries in project repos."
  default     = "" # TODO(you)
}

variable "actions_secrets" {
  type        = map(map(string))
  description = "Actions secrets per repo: { repo = { NAME = value } }. Values come from the vault via TF_VAR (mask tofu-apply)."
  sensitive   = true
  default     = {}
}

variable "actions_variables" {
  type        = map(map(string))
  description = "Plain (non-secret) Actions variables per repo: { repo = { NAME = value } }."
  default     = {}
}

variable "environment_name" {
  type        = string
  description = "Deployment environment gate used by infra-apply and sensitive workflows."
  default     = "production-infra"
}

variable "runner_group_name" {
  type        = string
  description = "Org-level runner group to create and restrict to the governed repos. Empty disables (user accounts cannot have runner groups)."
  default     = ""
  # TODO(verify): runner groups require a GitHub organization. Confirm account type.
}

variable "label_paths" {
  type        = list(string)
  description = "Path globs that trigger the needs-team-review label workflow."
  default     = ["**/sql/**", "**/migrations/**", "**/pipelines/**", "**/models/**", "docs/adr/**"]
}

variable "review_overrides" {
  type = map(object({
    approvals   = number
    code_owners = bool
  }))
  description = "Per-repo review-policy override: { repo = { approvals = n, code_owners = bool } }. Absent = strict default (1 approval + code-owner review). Relax only with a recorded ADR (0024 covers the solo-author infra repo)."
  default     = {}
}

variable "manage_label_workflow" {
  type        = bool
  description = "When true, commit the labeller workflow to a branch per repo (open the PR by hand; direct pushes to the protected branch would violate this very policy)."
  default     = false
}

variable "veggies_stack_password" {
  type        = string
  sensitive   = true
  description = "Serve password shared by github:true stacks (vault key veggies_stack_password, exported as TF_VAR_veggies_stack_password by scripts/tfvars_from_vault.py). Feeds each agent-kick block's VEGGIES_STACK_PASSWORD secret (ADR 0048)."
  default     = ""
}
