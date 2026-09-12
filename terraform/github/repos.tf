# Branch policy for every governed repo, as one ruleset per repo (merge queue is opt-in, ADR 0053).

locals {
  # The one literal; tests pin it to scripts/pr_review_gate.py's CONTEXT.
  pr_review_gate_context = "pr-review-agent"
}

data "github_repository" "this" {
  for_each = toset(var.repos)
  name     = each.key
}

resource "github_repository_ruleset" "main" {
  for_each   = toset(var.repos)
  name       = "main branch policy"
  repository = each.key
  target     = "branch"
  # No bypass_actors block: nobody - admins included - can bypass. This IS the
  # classic enforce_admins = true (rulesets have no admin exemption by default).
  enforcement = "active"

  conditions {
    ref_name {
      include = ["refs/heads/${var.default_branch}"]
      exclude = []
    }
  }

  rules {
    deletion                = true # == allows_deletions = false
    non_fast_forward        = true # == allows_force_pushes = false
    required_linear_history = true

    pull_request {
      required_approving_review_count = try(var.review_overrides[each.key].approvals, 1)
      require_code_owner_review       = try(var.review_overrides[each.key].code_owners, true) # <-- the bot can never satisfy this: it is not in CODEOWNERS
      dismiss_stale_reviews_on_push   = true
      # ADR 0024 (solo author): "someone other than the pusher" would be an
      # empty set on this repo, so the most-recent-push approval gate is off.
      # Re-enable once a second identity joins (then it is the right default).
      require_last_push_approval        = false
      required_review_thread_resolution = true # == require_conversation_resolution
    }

    required_status_checks {
      strict_required_status_checks_policy = true # == strict: branch must be up to date before merging
      dynamic "required_check" {
        for_each = toset(concat(
          lookup(var.required_checks_overrides, each.key, var.required_checks),
        contains(var.pr_review_gate_repos, each.key) ? [local.pr_review_gate_context] : []))
        content {
          context = required_check.value
          # The verdict check is pinned to the GitHub Actions app: only an App
          # token (the gate workflow's GITHUB_TOKEN) can create check runs, so
          # no classic PAT - e.g. the one every github:true stack holds (ADR
          # 0030) - can satisfy the required context with a forged commit
          # status. 15368 = github-actions (verified: gh api apps/github-actions).
          integration_id = required_check.value == local.pr_review_gate_context ? 15368 : null
        }
      }
    }

    # The merge queue is opt-in per repo (var.merge_queue_repos): a queue whose
    # required checks never report on merge_group runs stalls every queued
    # merge until the check timeout evicts it - the repo's CI must trigger on
    # merge_group first.
    dynamic "merge_queue" {
      for_each = contains(var.merge_queue_repos, each.key) ? [1] : []
      content {
        # REBASE matches house practice (main has no merge commits; the repo
        # allows squash+rebase only) and satisfies linear history.
        # Provider 6.13.0 requires the uppercase form.
        merge_method                      = "REBASE"
        grouping_strategy                 = "ALLGREEN"
        max_entries_to_build              = 5
        max_entries_to_merge              = 5
        min_entries_to_merge              = 1
        min_entries_to_merge_wait_minutes = 5
        check_response_timeout_minutes    = 60
      }
    }
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
