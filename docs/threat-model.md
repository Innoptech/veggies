# Threat model - garden

What an agent with a prompt injection can and cannot reach; what a leaked key
exposes. Mechanisms link to their ADRs.

## Actors and trust boundaries

| Identity | Runs | Trusted? | Egress |
|----------|------|----------|--------|
| `fedora` (admin) | ssh, opencode quadlet | yes (it's you) | unrestricted |
| `gh-runner` | ephemeral runner containers | **no** - executes untrusted PR content | proxy 3128 + litellm 4000 only (nftables per-UID) |
| `egress-proxy` | squid + litellm quadlets | yes (our config) | unrestricted |

Container escape from a runner lands in `gh-runner` - unprivileged, no sudo,
no docker daemon (rootless podman socket only), no other users' data, and the
nftables rules still apply. SELinux stays Enforcing.

## A prompt-injected job can

- Read the repo it runs on and its own work dir.
- Reach the squid allowlist (github, registries, mirrors, fireworks) -
  responses logged.
- Call litellm with the runner virtual key (revocable; spend-cappable).

## A prompt-injected job cannot (by design)

- Reach arbitrary hosts: nftables drop + log for the UID (`journalctl -k -g
  infra-egress-deny`).
- Hold the real Fireworks key: it exists only in the litellm container's env.
- Merge its own PR: branch protection requires a CODEOWNER review and the bot
  is never a code owner (ADR 0007); `production-infra` environment needs the
  human.
- Read the vault: vault content exists only as 0600 files of other users.
- Persist: ephemeral container, fresh registration token per job, work dir
  wiped by cleanup timers.

## Leaked-key exposure table

| Key | Where it lives | If leaked |
|-----|----------------|-----------|
| Fireworks API key | litellm container env (0600, vault-sourced) | Full model spend until rotated - one file to rekey, one converge to roll out |
| litellm runner virtual key | runner runtime.env | Spend through the proxy only, revocable, only useful from garden's 127.0.0.1/169.254.1.2 |
| litellm opencode virtual key | opencode auth.json | same, via the admin user's container |
| GitHub runner admin PAT/App | gh-runner api.env (0600) | Runner admin on the governed repos until revoked; never enters containers |
| Tailscale auth key | tailscale role (no_log) | Adds nodes with `tag:agent-host` until revoked in the tailnet console |
| restic password + S3 creds | backup role env | Can decrypt/delete the backup bucket; cannot reach the host |
| Vault password | `~/.config/infra/vault-password` (operator machine) | Everything above - protect accordingly (ADR 0004) |

## Remaining known gaps (accepted, documented)

- The tailnet can reach squid:3128 and litellm:4000 (firewalld trusted zone).
  Mitigation: tailnet ACLs (outside this repo) + litellm key auth + squid
  source ACLs.
- CrowdSec's value is bootstrap-window + visibility only while nothing public
  listens (ADR 0010).
- A determined agent can abuse the *allowlisted* domains themselves (e.g.
  commit secrets to a gist it creates with a stolen workflow token) - the
  mitigations are the review gate and the workflow token's minimal perms.
- garden stacks (ADR 0013/0014): each stack's litellm holds a copy of the
  Fireworks key (podman secret, per-stack random master key). A stack escape
  exposes that copy - revoke by `garden down --purge` + rotating in the vault.
- Remote clone of a private repo puts the vault's github_token in the VPS
  process list for the clone's duration (git http.extraHeader). Readable only
  to root/stacks on garden; accepted. TODO(verify): move to a credential
  helper or deploy keys later.
- Local stacks' egress is env-var enforced only (no nftables on a workstation)
  - the hard per-UID boundary exists on garden (ADR 0006). A local agent that
  unsets HTTPS_PROXY bypasses the proxy; treat local stacks as guardrails,
  not confinement.
