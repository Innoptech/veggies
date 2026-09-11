variable "repo" {
  type        = string
  description = "Repository name (owner comes from the provider config)."
}

variable "stack_host" {
  type        = string
  description = "Where the self-hosted runner reaches the repo's stack. host.containers.internal is the podman host gateway (NO_PROXY bypass in the runner quadlet, ADR 0033)."
  default     = "host.containers.internal"
}

variable "stack_port" {
  type        = number
  description = "The repo stack's published opencode port (`veggies ls`). A stack recreate on a new port = edit this block + apply (was: a hand-edited Actions variable)."
}

variable "stack_password" {
  type        = string
  sensitive   = true
  description = "The stack's serve password. On github:true stacks this is the vault key veggies_stack_password (ADR 0033), so re-ups never desync it."
}

variable "manage_files" {
  type        = bool
  description = "Deliver agent-trigger.yml + stack_kick.py on the delivery branch (merge the PR by hand). false only for the repo holding the master copies (this one)."
  default     = true
}

variable "files_branch" {
  type        = string
  description = "Delivery branch for the workflow/script files. Direct pushes to the protected default branch are rejected (labeller precedent: infra/needs-team-review)."
  default     = "infra/agent-trigger"
}

variable "workflow_content" {
  type        = string
  description = "Content of .github/workflows/agent-trigger.yml. The parent reads the master copy with file() - never templatefile(), the workflow is full of `$${{ }}`."
  default     = ""
}

variable "kick_script_content" {
  type        = string
  description = "Content of scripts/stack_kick.py, run by the workflow from the repo checkout."
  default     = ""
}
