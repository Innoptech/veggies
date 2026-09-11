# One-shot state migration (ADR 0044): the agent-task label and the VEGGIES_*
# secret/variables used to come from the generic machinery (repos.tf /
# secrets.tf, fed by the vault's actions_* maps). The agent-kick module owns
# them now; these moves keep the veggies repo's live objects attached - no
# destroy/create.
#
# PRECONDITION (operator, before the first plan/apply with this file): remove
# actions_secrets.veggies.VEGGIES_STACK_PASSWORD and
# actions_variables.veggies.VEGGIES_STACK_HOST / .VEGGIES_STACK_PORT from
# secrets/github.yml - otherwise tofu fails at plan with a moved-collision
# error naming the address. Delete this file after the first green apply.

moved {
  from = github_issue_label.agent_task["veggies"]
  to   = module.agent_kick_veggies.github_issue_label.agent_task
}

moved {
  from = github_actions_secret.this["veggies/VEGGIES_STACK_PASSWORD"]
  to   = module.agent_kick_veggies.github_actions_secret.stack_password
}

moved {
  from = github_actions_variable.this["veggies/VEGGIES_STACK_HOST"]
  to   = module.agent_kick_veggies.github_actions_variable.stack_host
}

moved {
  from = github_actions_variable.this["veggies/VEGGIES_STACK_PORT"]
  to   = module.agent_kick_veggies.github_actions_variable.stack_port
}
