---
status: accepted
date: 2026-09-10
---

# 0030. Opt-in GitHub write credentials in agent stacks

## Context and problem statement

Stacks (agent pods) could not push branches or open PRs: no credential ever
entered the pod. Clone-time auth is a transient `http.extraHeader` by
design - it exists for the clone's duration and is gone afterwards. That
holds only because the `-c` pair leads `clone` (command-scoped): a trailing
`-c` is git-clone's own `--config` and persists the header into the clone's
`.git/config` (ordering fixed and verified 2026-09-10, git 2.54; clones
from before may carry the header - see the threat model's known gaps).
Stacks are untrusted workloads (see the threat model), so write access must
not be ambient; but a stack that cannot push cannot do the daily work this
repo runs agents for.

## Decision drivers

- Write access must be opt-in per repo, never default-on.
- The token must not persist in the workspace: nothing in `.git/config`,
  nothing readable in the repo mount.
- Stacks are disposable; rotation must be a recreate, not a ceremony.
- ADR 0007 already makes the PR flow (branch protection, CODEOWNER review)
  the merge gate - the pod does not need to be the control.

## Decision

Opt-in per repo via `github: true` in veggies.yml (schema v1) or `--github`
at `up`. When enabled, the vault's `github_token` (bot PAT,
`secrets/github.yml`) is delivered as env `GH_TOKEN` via a per-stack podman
secret (stdin-only at up-time, like the litellm keys). Git authenticates
through a container-global credential helper that expands `$GH_TOKEN` at
use time - the token is never written to `.git/config`. `github-cli` ships
in the opencode image, so `gh` (which reads `GH_TOKEN`) works too. Commit
identity is `veggies-agent` / `veggies-agent@users.noreply.github.com`;
`git@github.com:` remotes are normalized to HTTPS.

Rejected alternative:

- **GitHub App installation tokens**: short-lived and finer-grained, but
  they need a mint-and-refresh dance the PAT does not. PAT chosen for
  simplicity; the App creds remain in `secrets/github.yml` for a later
  iteration.

## Consequences

- Positive: github-enabled stacks can push branches and open PRs as the
  bot, and the posture is visible: `up` prints
  `github:  GH_TOKEN + gh push/PR access enabled (ADR 0030)`.
- Negative / accepted: a prompt-injected session in a github-enabled stack
  can push branches and open PRs as the bot on every repo the PAT can
  reach. It cannot merge (branch protection + CODEOWNER review, ADR 0007) -
  the review gate is the merge control, not the pod. Rotation: vault-edit +
  stack recreate.
- The same PR also fixed the chained-squid DNS stall that made remote-stack
  git appear hung (~35s per CONNECT; verified 2026-09-10). That part is a
  bugfix, not a decision.
