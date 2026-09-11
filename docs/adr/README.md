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
| [0018](0018-mcp-server-components.md) | MCP server components | accepted |
| [0019](0019-agent-rosters-and-skills.md) | Agent rosters and skills: convention over machinery | accepted |
| [0020](0020-cloud-substrate-module.md) | Cloud substrate module (GCP / other) | proposed |
| [0021](0021-stack-data-backup-and-restore.md) | Stack data backup and restore | proposed |
| [0022](0022-cost-metering-and-model-routing.md) | Cost metering and model routing | accepted |
| [0023](0023-capability-model-dependency-reversal.md) | Capability model: contracts, not tools | accepted |
| [0024](0024-interim-access-and-identity-constraints.md) | Interim access and identity constraints (public SSH, backups off, relaxed self-review) | accepted |
| [0025](0025-stack-control-plane.md) | Stack control plane: Agent Canvas component driving opencode via ACP | superseded by 0028 |
| [0026](0026-retire-the-orchestrator.md) | Retire the orchestrator; supervision and automations move to the control plane | accepted |
| [0027](0027-second-harness-in-canvas.md) | Second harness in canvas; critic via our shim | superseded by 0028 |
| [0028](0028-retire-canvas-own-the-critic-loop.md) | Retire the canvas control plane; own the critic loop | accepted (amended by 0036) |
| [0029](0029-deny-over-ask-permission-envelope.md) | Deny-over-ask permission envelope for unattended sessions | accepted (amended by 0031) |
| [0030](0030-opt-in-github-write-credentials-in-stacks.md) | Opt-in GitHub write credentials in agent stacks | accepted |
| [0031](0031-no-ask-anywhere.md) | No ask anywhere: agent frontmatter joins the permission envelope | accepted |
| [0032](0032-dev-toolchain-in-harness-image.md) | Dev toolchain baked into the harness image | accepted (amended by 0047) |
| [0033](0033-issue-triggered-agent-kicks.md) | Issue-triggered agent kicks via GitHub Actions on the self-hosted runners | accepted |
| [0034](0034-session-observability.md) | Session observability: titled sessions, issue feedback, `veggies ui` | accepted (amended by 0046) |
| [0035](0035-one-shot-labels-and-done-guard.md) | One-shot labels and the done-guard | accepted (amended by 0046) |
| [0036](0036-always-on-critic-for-kicked-sessions.md) | Always-on critic for kicked sessions: in-pod supervisor component | accepted |
| [0037](0037-per-session-worktrees.md) | Per-session git worktrees inside the shared clone | accepted |
| [0038](0038-discussion-triggered-issue-distillation.md) | Discussion-triggered kicks: distill a discussion into issues | accepted |
| [0039](0039-discussion-feedback-contract.md) | Discussion feedback: agent-owned results, workflow-owned acks | accepted |
| [0040](0040-self-trigger-guard.md) | Self-trigger guard: bot exclusion, command-anchored keyword, in-flight done-guard | accepted (amended by 0043) |
| [0041](0041-discussion-elaboration-persona-roster.md) | Discussion elaboration: a persona POV roster answers /elaborate | accepted |
| [0042](0042-multi-role-plan-review.md) | Multi-role plan review: the persona roster reviews every kicked plan | accepted |
| [0043](0043-interim-shared-identity-trigger.md) | Interim: the shared olgam4 identity may trigger kicks | accepted (interim - sunsets at the GitHub App agent identity) |
| [0044](0044-live-first-watch-path-no-close-time-deletion.md) | Watch path lists live sessions first; no close-time session deletion | accepted |
| [0045](0045-repo-declared-verify-gate.md) | Repo-declared verify gate for kicked sessions | accepted |
| [0046](0046-draft-first-pr-lifecycle.md) | Draft-first PR lifecycle and the redefined done-guard | accepted |
| [0047](0047-networkless-lint-hooks.md) | Networkless gitleaks/actionlint hooks: image-baked pinned binaries, language: system | accepted |
| [0048](0048-agent-kick-install-as-one-module.md) | Install agent kicks as one terraform module block per repo | accepted |

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
