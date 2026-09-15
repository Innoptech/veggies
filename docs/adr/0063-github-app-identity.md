---
status: accepted
date: 2026-09-15
---

# 0063. GitHub App identity: rotating installation tokens replace the bot PAT everywhere

## Context and problem statement

Every GitHub credential this repo uses is one fine-grained personal access
token on the operator's own account, read from the vault key
`github_token` and delivered four ways: the OpenTofu github provider
(`GITHUB_TOKEN` exported by `mask tofu-plan/apply` and `infra-apply.yml`),
runner registration (`api.env` on the VPS, read by
`fetch_runner_token.py` before every runner start), the agent pod
(`GH_TOKEN` podman secret + git credential helper, ADR
[0030](0030-opt-in-github-write-credentials-in-stacks.md)), and the
remote clone/pull `http.extraHeader` (ADR
[0014](0014-remote-stacks-over-ssh.md)).

That shape was chosen under constraints that no longer hold. 0030 rejected
App installation tokens because "they need a mint-and-refresh dance the
PAT does not" and parked the App credentials in the vault "for a later
iteration". [0024](0024-interim-access-and-identity-constraints.md)
recorded that the operator could not create org-level resources, so the
bot *is* the operator's login; [0043](0043-interim-shared-identity-trigger.md)
then had to drop the trigger workflow's bot exclusion because bot and
human were the same identity; [0062](0062-pr-review-agent-comment-only.md)
named the ambient pod PAT as its accepted residual and bound it to issue
#56. On 2026-09-14 the operator, now an App manager in the org,
registered the GitHub App `veggies-harness` (App ID 4945586) owned by
Innoptech. The VPS was wiped on 2026-09-15 and a GCP substrate is planned,
so the from-zero bootstrap path is being re-proven now anyway.

## Decision drivers

- A bot identity distinct from any human: agent comments and PRs come from
  `veggies-harness[bot]`, so the trigger workflow can hard-stop them again
  (0043's sunset condition) and authorship is honest.
- Per-purpose, short-lived credentials: runner registration gets
  `administration:write` only; the clone header gets `contents:read` on one
  repository for one hour; the pod gets a write set scoped to its own
  repository, refreshed in place.
- No long-lived credential inside any container, and the App private key
  never enters the agent container.
- One implementation of the mint logic, boring dependencies
  (PyJWT + cryptography are Fedora and pip packages), tested once.
- A machine bootstrapped from zero with only the App in the vault must
  work end to end; the PAT is revoked, not merely unused.

## Considered options

- Keep the PAT, move it to a dedicated bot account.
- GitHub App with tokens minted once at `veggies up` (static env).
- GitHub App with a host-side refresh loop rewriting podman secrets.
- GitHub App with an in-pod sidecar serving tokens over loopback HTTP.
- GitHub App with an in-pod sidecar publishing tokens as a file on a shared
  emptyDir (chosen).

## Decision outcome

1. **One minter**: `scripts/github_app_token.py` signs a 9-minute RS256
   JWT and exchanges it at `POST /app/installations/{id}/access_tokens`,
   optionally narrowed by `repositories` and `permissions`. The runner role
   ships it as a sibling of `fetch_runner_token.py` (the role's `files/`
   entry is a git symlink to the script - the Molecule `collections.yml`
   precedent); the pod sidecar receives it as a stack-config file (the
   supervisor precedent); the CLI imports it directly.
2. **OpenTofu** authenticates through the provider's `app_auth` block fed
   by `TF_VAR_github_app_id / _installation_id / _private_key` - the vault
   keys `scripts/tfvars_from_vault.py` already exports. The
   `export GITHUB_TOKEN` lines go, replaced by `unset GITHUB_TOKEN`: a set
   token silently wins over `app_auth`.
3. **Runner registration**: the PEM lands as a 0600 file
   (`~gh-runner/.config/gh-runner/app.pem`, path in `api.env`);
   `fetch_runner_token.py` mints an installation token narrowed to
   `administration:write` on the runner's repository, then fetches the
   registration token exactly as before.
4. **Agent pod**: a `github-auth` sidecar, implied by `github: true` (no new
   `veggies.yml` key), holds the App credentials as the stack's podman
   secret, mints a token scoped to the stack's repository with a fixed
   write set, refreshes it twenty minutes before expiry, and writes it
   atomically to `/github-auth/token` on an emptyDir the opencode container
   mounts read-only. The git credential helper reads that file per
   invocation; a three-line `gh` wrapper exports it and execs the real `gh`.
   The sidecar discovers the bot login and id from the API and writes an
   `identity.gitconfig` that opencode includes, so commits are authored
   `veggies-harness[bot] <id+veggies-harness[bot]@users.noreply.github.com>`
   with zero constants in code or vault. Static `GH_TOKEN` env disappears.
5. **Remote clone/pull** on the operator machine mints a `contents:read`,
   single-repository token per operation instead of reading the PAT.
6. **Trigger workflow**: `github.event.comment.user.type != 'Bot'` on the
   comment branches and `github.event.sender.type != 'Bot'` on the label
   branch of `agent-trigger.yml`. Not on the `pull_request_target` ready
   path: the agent flips `gh pr ready` as the App by design (0062).
7. **Installation scope** is "selected repositories", maintained in the
   browser: the provider's `github_app_installation_repository` resource is
   documented as incompatible with App authentication.
8. **The PAT is revoked** and `github_token` removed from the vault, the
   example, the code, the tests and the docs. No compatibility guard: a
   dead key invites reuse.

Required App permissions, recorded here because a requested token must be
a subset of them: repository `Administration`, `Actions`, `Checks`,
`Contents`, `Discussions`, `Issues`, `Pull requests`, `Secrets`,
`Statuses`, `Variables`, `Workflows` (write), `Metadata` (read);
organization `Members` (read).

## Consequences

- Positive: the hard bot exclusion returns (0043 sunsets); App-authored
  commits, PRs and reviews; per-purpose tokens whose blast radius is one
  repository for one hour; `Discussions: write` finally present; rotation
  is "new key in the App settings -> vault-edit -> converge -> `veggies up`
  per github stack -> revoke the old key"; the threat model's clone-header
  gap closes (a one-hour read token instead of a long-lived write PAT in
  the process list).
- Negative / accepted trade-offs: one more sidecar image (python + PyJWT)
  per github-enabled stack; the installation repository list is a browser
  step; the token is still readable inside the agent container (the
  boundary is the App's permission set plus the repository scope, not
  secrecy from the agent); GitHub Apps cannot be CODEOWNERS, so 0024's
  review-policy constraint stands until a second human joins; any
  `GITHUB_TOKEN` in the operator's shell must be unset for tofu (the mask
  tasks do it).
- Status edits: 0030 and 0043 become `superseded by ADR-0063`;
  [0040](0040-self-trigger-guard.md), 0024 and 0062 gain notes. The
  deviation ledger does not change - the brief never chose PAT over App.

## Pros and cons of the options

### Dedicated bot account with a PAT

- Good, because nothing changes in the code.
- Bad, because the org cannot mint bot accounts for us, and the credential
  stays long-lived and ambient in every pod.

### Mint once at `veggies up`

- Good, because it is a one-line change to the existing secret plumbing.
- Bad, because installation tokens live one hour and kicked sessions
  routinely run longer; podman secrets are static env at container start.

### Host-side refresh loop rewriting podman secrets

- Good, because the private key stays on the host.
- Bad, because a running container never sees a rewritten secret; it would
  need a restart per hour.

### Sidecar serving tokens over loopback HTTP

- Good, because the agent container never holds the key.
- Bad, because it is a server (port, code, liveness), and busybox `wget`
  in the alpine base ignores `no_proxy` - every consumer would need the
  proxy-bypass flag. A file on a shared emptyDir has the same trust
  boundary with none of that.

### Sidecar publishing a token file (chosen)

- Good, because it reuses the supervisor's daemon shape (heartbeat mtime
  liveness, stack-config payload), needs no port, and the credential helper
  and `gh` wrapper read it per use so rotation is invisible to the agent.
- Bad, because the token is a readable file inside the pod; accepted, since
  the agent must be able to use it anyway.

## Links

- Issue #56 (GitHub App as identity); discussion #42.
- ADR 0014, 0024, 0030, 0040, 0043, 0062 (amended or superseded here).
- `scripts/github_app_token.py`, `cli/components/github_auth.py`,
  `deploy/github-auth/daemon.py`, `terraform/providers.tf`,
  `ansible/roles/github_runner/`.
