# ADR 0044: one instance per repo is the whole GitHub-side install of agent
# kicks - the trigger workflow + kick script (delivered on a side branch;
# merge the PR), the agent-task label, and the stack coordinates the
# workflow needs (host/port variables, password secret). No new long-lived
# processes, no inbound ports: the event path stays outbound-poll (ADR 0033).

# The delivery branch must exist before the file resources commit to it -
# github_repository_file does not create branches (verified against provider
# v6.13.0). github_branch's create swallows 422 already-exists and its read
# drops state on 404, so a branch deleted after its PR merged is recreated
# from the default-branch tip on the next delivery (the ADR 0044 lifecycle).
resource "github_branch" "delivery" {
  count         = var.manage_files ? 1 : 0
  repository    = var.repo
  branch        = var.files_branch
  source_branch = var.source_branch
}

resource "github_repository_file" "agent_trigger" {
  count          = var.manage_files ? 1 : 0
  repository     = var.repo
  branch         = var.files_branch
  file           = ".github/workflows/agent-trigger.yml"
  content        = var.workflow_content
  commit_message = "ci: agent-trigger workflow (managed by the infra repo, ADR 0044)"
  # true: post-merge a fresh delivery branch inherits the merged file -
  # overwriting it on the DELIVERY branch is the update path; the delivery
  # PR's diff is the clobber guard, since nothing reaches the protected
  # default branch without a human merge.
  overwrite_on_create = true

  depends_on = [github_branch.delivery]
}

resource "github_repository_file" "stack_kick" {
  count               = var.manage_files ? 1 : 0
  repository          = var.repo
  branch              = var.files_branch
  file                = "scripts/stack_kick.py"
  content             = var.kick_script_content
  commit_message      = "ci: stack_kick.py for the agent-trigger workflow (managed by the infra repo, ADR 0044)"
  overwrite_on_create = true

  # The branch first, then the two files in order.
  depends_on = [github_branch.delivery, github_repository_file.agent_trigger]
}

resource "github_issue_label" "agent_task" {
  repository  = var.repo
  name        = "agent-task"
  color       = "0E8A16"
  description = "Hand this issue to the veggies agent stack (agent-trigger workflow)."
}

resource "github_actions_secret" "stack_password" {
  repository      = var.repo
  secret_name     = "VEGGIES_STACK_PASSWORD"
  plaintext_value = var.stack_password

  lifecycle {
    precondition {
      condition     = var.stack_password != ""
      error_message = "stack_password is empty - run via mask tofu-plan/tofu-apply so the vault feeds TF_VAR_veggies_stack_password."
    }
  }
}

resource "github_actions_variable" "stack_host" {
  repository    = var.repo
  variable_name = "VEGGIES_STACK_HOST"
  value         = var.stack_host
}

resource "github_actions_variable" "stack_port" {
  repository    = var.repo
  variable_name = "VEGGIES_STACK_PORT"
  value         = tostring(var.stack_port)

  lifecycle {
    precondition {
      condition     = var.stack_port > 0
      error_message = "stack_port must be the stack's published port (veggies ls) - the roster block's TODO(you) placeholder must be replaced before plan/apply."
    }
  }
}
