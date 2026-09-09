# Architecture decision records

New decisions: copy `0000-madr-template.md`, next free number, one topic per
file. Never edit a decided ADR - write a new one that supersedes it. ADRs are
append-only history: they record *why* a decision was taken. The system as it
is today lives in [../architecture.md](../architecture.md); operational
procedures in [../runbook.md](../runbook.md). Keep both current as reality
changes, and keep this index's titles and statuses in sync.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted |
| [0002](0002-public-cloud-over-vps.md) | OVH Public Cloud over VPS (target architecture) | accepted (deferred) |
| [0003](0003-tailscale-only-access.md) | Tailscale-only network access | accepted |
| 0004 | Secrets in git via ansible-vault (supersedes the brief's sops+age) | accepted |
| [0005](0005-ephemeral-containerised-runners.md) | Ephemeral containerised GitHub runners | accepted |
| [0006](0006-egress-allowlist.md) | Egress allowlist: squid proxy + per-UID nftables | accepted |
| [0007](0007-github-policy-as-code.md) | GitHub policy as code | accepted |
| [0008](0008-interim-platform-vps-fedora-local-state.md) | Interim platform: manually-rented VPS, Fedora 44, local state | accepted |
| 0009 | Rootless Podman + Quadlet instead of Docker | accepted |
| [0010](0010-crowdsec-auditd-no-fail2ban.md) | CrowdSec + auditd instead of fail2ban | accepted |
| [0011](0011-litellm-gateway-model-routing.md) | LiteLLM gateway as the model router; keys held only by the proxy | accepted |
| [0012](0012-agent-config-baseline-superpowers.md) | Vendored agent-config baseline; Superpowers as a pinned opencode plugin | accepted |
| [0013](0013-repo-scoped-agent-stacks.md) | Repo-scoped agent stacks via the `veggies` CLI (renamed from garden, 0015) | accepted |
| [0014](0014-remote-stacks-over-ssh.md) | Remote stacks over ssh; CLI owns stacks, Ansible owns the host | accepted |
| [0015](0015-rename-garden-to-veggies.md) | Project identity renamed: garden -> veggies | accepted |
| [0016](0016-substrate-vs-stack-boundary.md) | Ansible is the substrate; the CLI is the stack | accepted |
| [0017](0017-agent-orchestrator-and-workflows.md) | Agent orchestrator and adaptive pipelines | superseded by 0026 |
| [0018](0018-mcp-server-components.md) | MCP server components | proposed |
| [0019](0019-agent-rosters-and-skills.md) | Agent rosters and skills: convention over machinery | accepted |
| [0020](0020-cloud-substrate-module.md) | Cloud substrate module (GCP / other) | proposed |
| [0021](0021-stack-data-backup-and-restore.md) | Stack data backup and restore | proposed |
| [0022](0022-cost-metering-and-model-routing.md) | Cost metering and model routing | proposed |
| [0023](0023-capability-model-dependency-reversal.md) | Capability model: contracts, not tools | accepted |
| [0024](0024-interim-access-and-identity-constraints.md) | Interim access and identity constraints (public SSH, backups off, relaxed self-review) | accepted |
| [0025](0025-stack-control-plane.md) | Stack control plane: Agent Canvas component driving opencode via ACP | accepted |
| [0026](0026-retire-the-orchestrator.md) | Retire the orchestrator; supervision and automations move to the control plane | accepted |

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
| Tailscale-only access | Hardened public SSH (no Innoptech tailnet yet) | ADR 0003/0024 |
| Backups from day one | backup role gated off until a bucket exists | ADR 0024 |
| Human review + code owners on every repo | infra repo merges need checks only (solo author) | ADR 0007/0024 |
