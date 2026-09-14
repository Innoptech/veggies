---
status: accepted (amended by 0048)
date: 2026-09-10
---

# 0033. Issue-triggered agent kicks via GitHub Actions on the self-hosted runners

## Context and problem statement

The project's goal is an always-running agent that reacts to this repo's
issues/discussions. Until now nothing listened: 0026 retired the
orchestrator and deferred event triggers "while inbound listeners are out
of posture" (0024), and no poller or webhook ever landed. The goal needs
an event path that (a) adds no inbound listener on the VPS, (b) reuses
what already exists, (c) keeps the stack's API password from going stale.

## Decision drivers

- Posture: the VPS stays inbound-dark (0024). GHA fits because the
  self-hosted runners *poll* GitHub outbound-only; GitHub's infrastructure
  is the listener.
- The ephemeral runners (0005) already run on the VPS with proxy access
  and a NO_PROXY path to host-published services
  (`host.containers.internal`).
- A kick must not hold a runner for a whole agent run - the opencode call
  must be async.
- Re-ups regenerate stack secrets; a GitHub-stored copy of the serve
  password must not rot.

## Decision

`.github/workflows/agent-trigger.yml` (in the governed repo) runs on
`[self-hosted, linux, x64, veggies]` when an issue is labeled
`agent-task` (tofu-managed label) or a trusted comment
(OWNER/MEMBER/COLLABORATOR) contains `/opencode`. The job runs
`scripts/stack_kick.py` (stdlib-only, pytest-covered), which creates a
session on the repo's long-lived stack and fires the issue prompt via
`POST /session/{id}/prompt_async` (returns 204 immediately; verified live
against opencode 1.18.27 - the newer `/api/session/{id}/prompt` only
*admits* to a queue and never starts an idle session). The stack URL is
`http://host.containers.internal:<port>`; host + port travel as Actions
variables, the serve password as an Actions secret, all tofu-managed and
vault-fed (ADR 0007 path).

The serve password source changes to support this: on `github: true`
stacks the opencode password is `VaultKey("veggies_stack_password")`
instead of per-stack `Generated`, so the value tofu publishes and the
value the stack serves are the same vault key and re-ups never
desynchronize them. Non-github stacks keep per-stack random passwords.

Supervision stays operator-invoked (0028): the workflow kicks, a human
watches in the web UI, `veggies supervise` judges.

## Consequences

- Positive: the always-on loop exists with zero new long-lived processes
  and zero inbound ports; the runner fleet is dogfooded too.
- Negative/accepted: prompt-injection surface - issue text is model input
  and the agent holds write credentials. Mitigated by repo visibility
  (private org repo) and the author-association gate on comments;
  discussions remain unhandled (GraphQL; follow-up if wanted).
- Accepted: `concurrency` serializes kicks per issue; two runners mean a
  long kick never blocks CI (kicks are milliseconds).
- The stack must be up and its port must match the Actions variable;
  `veggies ls` shows it, and a stale port fails the workflow loudly.
- Open at adoption (2026-09-10): the bot's fine-grained PAT lacks the
  Actions `Secrets: write` / `Variables: write` repository permissions, so
  `tofu apply` 403s on the secret/variable resources (the label applied).
  TODO(you): grant those two permissions on Innoptech/veggies and re-run
  `mask tofu-apply`. Until then, run `scripts/stack_kick.py` by hand
  (runbook §9) - same code path the workflow uses.
- Also found at adoption: `terraform/github/secrets.tf`'s `for_each` over
  a sensitive local never worked with non-empty input; fixed by iterating
  de-sensitized keys (the values stay sensitive).

## Links

- Builds on: [0005](0005-ephemeral-containerised-runners.md),
  [0024](0024-interim-access-and-identity-constraints.md),
  [0030](0030-opt-in-github-write-credentials-in-stacks.md)
- Supersedes the "event triggers stay off" stance of
  [0026](0026-retire-the-orchestrator.md) for the outbound-poll case.
