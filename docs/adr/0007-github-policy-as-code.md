---
status: accepted
date: 2026-09-03
---

# 0007. GitHub policy as code

## Context and problem statement

Coding agents will open pull requests in the project repositories, triggered
from issue/PR comments. The guarantee "an agent cannot merge its own work"
must hold even if every human forgets it exists - so it must be expressed as
reviewable code, not as clicked settings in the GitHub UI.

## Decision drivers

- The guarantee must survive account changes and be diffable in review.
- The bot's approval must never satisfy the review requirement.
- The mechanism must work without assuming a paid GitHub plan.
- Secrets for workflows must come from the vault, never from tfvars.

## Considered options

- `github` provider with classic `github_branch_protection`
- `github` provider with `github_repository_ruleset`
- Manual settings documented in a runbook

## Decision outcome

Classic `github_branch_protection` per repo (module `terraform/github/`):
PRs required, one approving **code-owner** review (`require_code_owner_reviews`
- the bot is never a code owner, so its approval never counts), stale-review
dismissal, last-push approval, linear history, no force push or deletion,
`enforce_admins = true`, conversation resolution required. Plus: a
human-gated `production-infra` environment per repo (`can_admins_bypass =
false`), the `needs-team-review` label + labeller workflow (shipped on a side
branch, merged by hand), Actions secrets/variables fed from the vault, and an
optional org runner group.

## Consequences

- Positive: the merge policy is a PR-reviewable diff; state is reproducible
  with `tofu apply`; secrets flow from the vault only.
- Negative: Actions secret values land in the local tofu state (accepted;
  state is gitignored and restic-backed-up - see ADR 0008).
- Each project repo needs a `CODEOWNERS` file naming the human.

## Pros and cons of the options

### Classic branch protection

- Good: works on all repo/plan types; mature provider resource.
- Bad: older API; fewer knobs than rulesets.

### Repository rulesets

- Good: newer, richer (bypass actors etc.).
- Bad: feature availability depends on plan/repo visibility
  (TODO(verify) before a future migration); no immediate need.

### Manual settings

- Good: zero code.
- Bad: exactly the failure mode this ADR exists to kill.

## Links

- [terraform/github/](../../terraform/github/README.md)
- ADR 0004 (vault secrets), ADR 0008 (local state)
