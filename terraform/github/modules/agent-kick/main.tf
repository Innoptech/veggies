# ADR 0044: one instance per repo is the whole GitHub-side install of agent
# kicks - the trigger workflow + kick script (delivered on a side branch;
# merge the PR), the agent-task label, and the stack coordinates the
# workflow needs (host/port variables, password secret). No new long-lived
# processes, no inbound ports: the event path stays outbound-poll (ADR 0033).

resource "github_repository_file" "agent_trigger" {
  count               = var.manage_files ? 1 : 0
  repository          = var.repo
  branch              = var.files_branch
  file                = ".github/workflows/agent-trigger.yml"
  content             = var.workflow_content
  commit_message      = "ci: agent-trigger workflow (managed by the infra repo, ADR 0044)"
  overwrite_on_create = false # never clobber a repo's own file silently
}

resource "github_repository_file" "stack_kick" {
  count               = var.manage_files ? 1 : 0
  repository          = var.repo
  branch              = var.files_branch
  file                = "scripts/stack_kick.py"
  content             = var.kick_script_content
  commit_message      = "ci: stack_kick.py for the agent-trigger workflow (managed by the infra repo, ADR 0044)"
  overwrite_on_create = false

  # Both files commit to the same (initially non-existent) branch; serialize
  # so concurrent branch creation cannot race.
  depends_on = [github_repository_file.agent_trigger]
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
}
