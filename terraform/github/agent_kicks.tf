# ADR 0044: the roster of repos the agent serves. Installing agent kicks on
# a repo is ONE module block here - it declares everything the kick path
# needs on the GitHub side: the workflow + kick script (delivered on the
# repo's `infra/agent-trigger` branch - merge that PR once), the agent-task
# label, and the stack coordinates (VEGGIES_STACK_HOST/PORT variables,
# VEGGIES_STACK_PASSWORD secret). Substrate (VPS, podman, runner
# registration, the stack itself, the vault) stays manual - docs/runbook.md.
#
# The master copies are this repo's own .github/workflows/agent-trigger.yml
# and scripts/stack_kick.py, read with file() - NEVER templatefile(): the
# workflow is full of `$${{ }}`, which templatefile would interpolate.

module "agent_kick_veggies" {
  source = "./modules/agent-kick"

  repo           = "veggies"
  stack_port     = 0 # TODO(you): the live port (`veggies ls`) - must match before apply
  stack_password = var.veggies_stack_password

  # This repo holds the master copies by definition - tofu must not own
  # files that PRs edit here.
  manage_files = false

  workflow_content    = file("${path.module}/../../.github/workflows/agent-trigger.yml")
  kick_script_content = file("${path.module}/../../scripts/stack_kick.py")
}
