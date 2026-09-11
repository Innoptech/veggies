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
| Network | Public SSH, key-only + CrowdSec-guarded (Tailscale-only deferred - ADR 0024) |
| Egress | squid proxy + per-UID nftables; agents reach an allowlist only |
| Models | per-stack LiteLLM in the pod; the Fireworks key is held only by the proxy (podman secret); agents get revocable virtual keys |
| Secrets | ansible-vault files committed encrypted to git |
| State | local, gitignored, restic-backed-up |
| Backups | restic to OVH Object Storage (deferred until a bucket exists - ADR 0024) |

## Stacks

A stack is one repo's agent environment: a rootless pod with opencode-serve
(harness), litellm (model router) and squid (egress), plus opt-in MCP
sidecars on pod loopback (ADR 0018). The opencode port publishes on
127.0.0.1 locally, tailnet-only on the VPS; litellm and squid are
pod-internal. Stacks are defined, rendered and owned solely by the
`veggies` CLI; Ansible prepares the host and nothing more. Supervision is
operator-invoked from the CLI: `veggies supervise` runs the critic loop
against opencode sessions (ADR 0028).

Components implement capability contracts (`cli/capabilities.py`) and are
selected per repo via `veggies.yml`; per-repo agent rosters and skills are
discovered from `.opencode/` in the mounted repo (ADR 0019). Stacks may opt
into GitHub write access (`github: true` in veggies.yml): the pod carries
the bot PAT as `GH_TOKEN` + `gh` (ADR 0030) and takes its serve password
from the vault (ADR 0033). The opencode image also carries the dev
toolchain (python/mask/ansible/tofu/tflint - ADR 0032) so agents run the
repo's own checks in-pod; molecule is excluded (no podman socket, ADR
0028).

Event path (ADR 0033): `.github/workflows/agent-trigger.yml` on the
self-hosted runners kicks this repo's stack on `agent-task` labels /
`/opencode` comments via `scripts/stack_kick.py`; runners reach the stack
API over the host gateway, allowed by the egress role's per-user dport
exceptions. No inbound listener on the VPS. Observability (ADR 0034):
kicked sessions are titled `#N: <issue>`, the workflow comments the session
link back onto the issue, and operators watch via `veggies ui` (ssh tunnel
helper) / `veggies sessions` / the web UI.

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
deploy/          Containerfiles + component payloads (MCP toolbox server)
scripts/         tfvars_from_vault.py, vault_get.py, stack_kick.py
tests/           pytest suite + machine-generated golden pod.yaml
docs/            architecture.md, runbook.md, threat-model.md, adr/
.github/workflows/  infra-ci.yml, agent-trigger.yml
```
