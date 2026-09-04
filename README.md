# infra

Infrastructure-as-code and configuration management for **veggies**: an
always-on OVHcloud VPS that hosts autonomous coding agents (opencode) and
ephemeral self-hosted GitHub Actions runners, reachable only over Tailscale.

Everything about the machine - access, secrets handling, network policy,
GitHub-side merge policy - is reviewable code in this repo. Project-specific
agent instructions (`AGENTS.md`, project skills) live in the project repos;
the shared agent baseline lives in `agent-config/` (phase 6, ADR 0012).

## Status

Phases 1-14 done. The infra is implemented; live values are placeholders until you fill them (see below). The `veggies` CLI (repo-scoped agent stacks, ADR 0013/0014) is implemented and verified live locally.

## Stacks: the daily driver (`veggies`, ADR 0013)

One stack = one repo = one pod: opencode-serve + litellm + squid, rootless,
persistent (quadlet + watchdog), locally or on the VPS.

```bash
mask veggies-install          # once: puts `veggies` in ~/.local/bin
cd ~/code/some-repo
veggies up                    # prompts, builds, starts, attaches
veggies ls                    # all stacks, live status, persistence
veggies attach <name>         # back into a running stack
veggies logs <name> [-f]      # pod logs (or a single container)
veggies down <name>           # stop (keeps volumes/secrets/state)
veggies down <name> --purge   # delete everything for that stack
veggies --host veggies up --clone https://github.com/you/repo.git  # on the VPS
```

- Only the opencode port is published (127.0.0.1 locally, tailnet-only on the
  VPS via firewalld); litellm and squid are pod-internal.
- Mount mode bind-mounts your checkout rw and relabels it
  `container_file_t` (harmless for your user; that's the `:z` equivalent).
  `--clone` keeps the clone inside the veggies state dir instead.
- Env overrides for scripts: `VEGGIES_REPO VEGGIES_NAME VEGGIES_HOST
  VEGGIES_CLONE=1 VEGGIES_YES=1 VEGGIES_NO_ATTACH=1 VEGGIES_NO_INSTALL=1`.

### veggies.yml (per-repo customization, ADR 0016)

A repo can declare its stack with an optional `veggies.yml` at its root —
versioned with the repo it describes. Schema v0:

```yaml
model: kimi-k3          # litellm alias; becomes the stack's default model
components: [opencode, litellm, squid]  # default: all three (the core stack)
```

Precedence: `--model` flag > veggies.yml > vendored default. Unknown keys
warn and are ignored (so future schema versions degrade gracefully); invalid
values abort with an error.
- Boot persistence locally needs linger once: `sudo loginctl enable-linger
  $USER` (the CLI warns you).

## Architecture at a glance

| | Interim (today) | Target (recorded direction) |
|---|---|---|
| Compute | Manually-rented OVH VPS `veggies` (6 vCPU / 12 GB), Fedora 44 | OVH Public Cloud via OpenTofu module (scaffolded, gated) - ADR 0002/0008 |
| IaC | OpenTofu 1.12 (github module from phase 2) | + openstack module when migrating |
| Config mgmt | Ansible 2.21, roles + `site.yml`, Molecule (podman) per role | same |
| Containers | Rootless Podman + Quadlet, users `fedora` / `gh-runner` / `egress-proxy` (ADR 0009) | same |
| Network | Tailscale-only SSH; public SSH closed after bootstrap (ADR 0003) | same |
| Egress | squid proxy + per-UID nftables; agents reach an allowlist only (ADR 0006) | same |
| Models | per-stack LiteLLM in the pod; Fireworks key held only by the proxy (podman secret); agents get revocable virtual keys (ADR 0011) | same |
| Secrets | ansible-vault files committed to git (ADR 0004) | same |
| State | local, gitignored, restic-backed-up | OVH S3-compatible backend (scaffolded) |
| Backups | restic to OVH Object Storage (phase 8) | same |

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| mask | 0.11.x | already on this workstation |
| tofu | 1.12.6 | release tarball -> `~/.local/bin` (checksum-verified) |
| gitleaks / tflint / actionlint | 8.30.1 / 0.64.0 / 1.7.12 | same |
| python | 3.14 | system |
| ansible-core / ansible-lint / molecule / pre-commit / yamllint / pytest | pinned in `requirements-dev.txt` | `mask setup` |
| podman | 5.8.x | system (workstation + veggies are Fedora 44) |

## Setup

```bash
git clone <this-repo> && cd veggie   # repo dir name may differ; this is the repo root
mask setup                           # venv + pinned tools + pre-commit hooks
mkdir -p ~/.config/infra
chmod 700 ~/.config/infra
$EDITOR ~/.config/infra/vault-password && chmod 600 ~/.config/infra/vault-password
```

## Daily tasks

`mask help` shows everything. Common ones:

| Task | Does |
|------|------|
| `mask ci` | everything CI runs, locally |
| `mask precommit` | all pre-commit hooks (gitleaks, yamllint, actionlint, tofu fmt, vault check) |
| `mask tofu-plan` / `mask tofu-apply` | plan is read-only; apply requires typing `apply` (live-account mutation) |
| `mask vault-edit secrets/model.yml` | create/edit encrypted secrets |
| `mask vault-check` | fail if any secrets file is plaintext |
| `mask molecule-test <role>` / `mask molecule-all` | role tests (phase 4+) |
| `mask converge` / `mask bootstrap` | Ansible against veggies (phase 4+) |

## Secrets workflow (ansible-vault, ADR 0004)

Three domain files, all committed **encrypted** (pre-commit + CI enforce):

| File | Holds | Consumed by |
|------|-------|-------------|
| `secrets/model.yml` | Fireworks key, litellm master key | injected per stack by `veggies` (podman secrets) |
| `secrets/github.yml` | GitHub App creds or bot PAT | tofu github module, runner registration |
| `secrets/infra.yml` | tailscale auth key, restic password + S3 creds | tailscale/backup roles |

Structure templates: `secrets/*.yml.example`. Rules: never decrypt to disk;
tofu consumes secrets via env-only export (`scripts/tfvars_from_vault.py`);
rotation procedures live in `docs/runbook.md` section 3.

## Layout

```
README.md  AGENTS.md  maskfile.md  requirements-dev.txt
.pre-commit-config.yaml  .ansible-lint  .yamllint  .tflint.hcl
ansible/         cfg, inventory (veggies), group_vars example, requirements.yml
                 playbooks/ (phase 4+), roles/ (phase 4+), molecule per role
terraform/       root module; github/ (phase 2), ovh/ (phase 3, scaffold-only)
secrets/         ansible-vault files (+ .example templates)
agent-config/    (phase 6) opencode.json, agents/, skills/, litellm/
scripts/         tfvars_from_vault.py (+ tests/)
docs/            adr/ (0000 template, 0001, index), runbook.md, threat-model.md (phase 7)
.github/workflows/ infra-ci.yml (infra-apply.yml in phase 9)
```

## Deviation ledger (vs the original brief)

| Brief said | We do | Why / where recorded |
|------------|-------|----------------------|
| Ubuntu 24.04 LTS | Fedora 44 (VPS image, already ordered) | ADR 0008 |
| Public Cloud via tofu | VPS rented manually; compute module scaffold-only | ADR 0002/0008 |
| Remote S3 state | Local state for now; backend scaffolded | ADR 0008 |
| sops + age | ansible-vault | ADR 0004 |
| ufw | firewalld (Fedora-native) | ADR 0008 |
| fail2ban | CrowdSec + nftables bouncer + auditd | ADR 0010 |
| Docker daemon | rootless Podman + Quadlet, per-user | ADR 0009 |
| (no model router) | per-stack LiteLLM in the pod | ADR 0011/0013 |
| Agent config only in project repos | + vendored baseline `agent-config/` with Superpowers pinned | ADR 0012 |

## Placeholders to fill (before the noted phase)

Committed as `TODO(you)` markers in the example files:

- age/vault: `~/.config/infra/vault-password` (phase 4)
- `secrets/*.yml`: Fireworks key, litellm keys, GitHub App/PAT, tailscale auth key, restic creds (phases 2/4/6/8)
- `terraform.tfvars`: `github_owner`, `github_repos` (phase 2)
- `group_vars/all.yml`: tailnet name + ACL tag, ssh public keys, github owner/repos, restic bucket endpoint, optional webhook (phases 2-8)
- VPS disk size (informs phase 8 cleanup thresholds) - note it in `all.yml`

## Docs

- ADRs: [docs/adr/README.md](docs/adr/README.md)
- Runbook (stubs until each phase): [docs/runbook.md](docs/runbook.md)
- Threat model: [docs/threat-model.md](docs/threat-model.md)

## Conventions

- `TODO(you)` = human must supply a value. `TODO(verify)` = uncertain upstream
  detail; check the linked docs before relying on it.
- Conventional commits, small and single-purpose.
- Agents working in this repo: read [AGENTS.md](AGENTS.md) first.
