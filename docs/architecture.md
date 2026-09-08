# Architecture

The system as it is today. This is a living document - update it when the
system changes. The *reasons* behind each choice live in the ADRs
([adr/](adr/README.md)); the planned evolution (OVH Public Cloud, remote
state) lives in ADR 0002/0020.

## The machine

| Area | Implementation |
|------|----------------|
| Compute | Manually-rented OVH VPS `veggies` (6 vCPU / 12 GB), Fedora 44 |
| IaC | OpenTofu 1.12; `terraform/github/` is live, `terraform/ovh/` is a gated scaffold |
| Config mgmt | Ansible 2.21, roles + `site.yml`, Molecule (podman) per role |
| Containers | Rootless Podman + Quadlet; system users `fedora` / `gh-runner` / `egress-proxy` / `stacks` |
| Network | Tailscale-only SSH; public SSH closed at bootstrap |
| Egress | squid proxy + per-UID nftables; agents reach an allowlist only |
| Models | per-stack LiteLLM in the pod; the Fireworks key is held only by the proxy (podman secret); agents get revocable virtual keys |
| Secrets | ansible-vault files committed encrypted to git |
| State | local, gitignored, restic-backed-up |
| Backups | restic to OVH Object Storage |

## Stacks

A stack is one repo's agent environment: a rootless pod with opencode-serve
(harness), litellm (model router) and squid (egress), optionally the
orchestrator (workflows). Only the opencode port is published - 127.0.0.1
locally, tailnet-only on the VPS; litellm and squid are pod-internal. Stacks
are defined, rendered and owned solely by the `veggies` CLI; Ansible prepares
the host and nothing more.

Components implement capability contracts (`cli/capabilities.py`) and are
selected per repo via `veggies.yml`; per-repo agent rosters and skills are
discovered from `.opencode/` in the mounted repo (ADR 0019).

## Repo layout

```
README.md        pitch + quickstart - kept short on purpose
AGENTS.md        non-negotiable rules for agents working here
maskfile.md      task runner (mask 0.11.x); CI invokes tools directly
requirements-dev.txt / requirements.yml / .pre-commit-config.yaml
ansible/         cfg, inventory (veggies), group_vars, playbooks, roles,
                 molecule scenario per role
terraform/       root module; github/ (live), ovh/ (scaffold-only)
secrets/         ansible-vault files (+ .example templates)
agent-config/    vendored agent baseline: opencode.json, agents/, skills/,
                 litellm/
cli/             the veggies CLI: veggies.py, veggies_stack.py,
                 capabilities.py, components/
deploy/          Containerfiles + the orchestrator payload
scripts/         tfvars_from_vault.py, vault_get.py
tests/           pytest suite + machine-generated golden pod.yaml
docs/            architecture.md, runbook.md, threat-model.md, adr/
.github/workflows/  infra-ci.yml
```
