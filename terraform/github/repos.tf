# Branch protection and labels for every governed repo.

data "github_repository" "this" {
  for_each = toset(var.repos)
  name     = each.key
}

resource "github_branch_protection" "main" {
  for_each      = toset(var.repos)
  repository_id = data.github_repository.this[each.key].node_id
  pattern       = var.default_branch

  # The guarantees, as code:
  enforce_admins                  = true # admins are NOT exempt
  required_linear_history         = true
  allows_force_pushes             = false
  allows_deletions                = false
  require_conversation_resolution = true

  required_status_checks {
    strict   = true # branch must be up to date before merging
    contexts = lookup(var.required_checks_overrides, each.key, var.required_checks)
  }

  required_pull_request_reviews {
    required_approving_review_count = try(var.review_overrides[each.key].approvals, 1)
    require_code_owner_reviews      = try(var.review_overrides[each.key].code_owners, true) # <-- the bot can never satisfy this: it is not in CODEOWNERS
    dismiss_stale_reviews           = true
    # ADR 0024 (solo author): "someone other than the pusher" would be an
    # empty set on this repo, so the most-recent-push approval gate is off.
    # Re-enable once a second identity joins (then it is the right default).
    require_last_push_approval = false
  }
}

resource "github_issue_label" "needs_team_review" {
  for_each    = toset(var.repos)
  repository  = each.key
  name        = "needs-team-review"
  color       = "B60205"
  description = "Touches sensitive paths (see workflow); requires a human review before merge."
}

# The labeller workflow is committed to a side branch, never to the protected
# branch directly. Open and merge the PR by hand (docs/runbook.md).
# Latent gap (do NOT fix here): the side branch must pre-exist - when
# manage_label_workflow is next enabled, port the agent-kick module's
# github_branch pattern (modules/agent-kick/main.tf, ADR 0048).
resource "github_repository_file" "needs_team_review_workflow" {
  for_each            = var.manage_label_workflow ? toset(var.repos) : toset([])
  repository          = each.key
  branch              = "infra/needs-team-review"
  file                = ".github/workflows/needs-team-review.yml"
  content             = templatefile("${path.module}/templates/needs-team-review.yml", { paths = var.label_paths })
  commit_message      = "ci: add needs-team-review labeller (managed by infra repo)"
  overwrite_on_create = false
}
