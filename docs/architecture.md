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
selected per repo via `veggies.yml`. The repo's own agent-instruction file
(AGENTS.md or CLAUDE.md, first match walking up from the workspace), its
agents and its skills (`.opencode/`, `.claude/`/`.agents/` compat paths)
are discovered from the mounted repo with project-over-global precedence
(ADR 0019) - zero veggies.yml keys for any of it. The no-ask permission
envelope spans the merged project+global config (ADR 0049): pytest
enforces it over the vendored tiers and this repo's own, and
`scripts/stack_kick.py` refuses to kick a repo whose checked-out project
tier carries `ask`. Stacks may opt
into GitHub write access (`github: true` in veggies.yml): the pod carries
the bot PAT as `GH_TOKEN` + `gh` (ADR 0030) and takes its serve password
from the vault (ADR 0033). The opencode image also carries the dev
toolchain (python/mask/ansible/tofu/tflint/gitleaks/actionlint - ADR
0032/0047) so agents run the repo's own checks in-pod; molecule is
excluded (no podman socket, ADR 0028).

Cost metering (ADR 0022/0051): the litellm router writes one JSON line
per model call to `<state_root>/<stack>/costs/costs.jsonl` on the host -
size-rotated, fail-open, each record stamped with the calling session's
title/id by the harness plugin and the judge paths. That cost log is new
durable stack state: it survives `down`/`up`/`sync` and sits inside the
backup role's `backup_paths`.

Event path (ADR 0033/0038/0041/0050): `.github/workflows/agent-trigger.yml`
on the self-hosted runners kicks this repo's stack via
`scripts/stack_kick.py` on `agent-task` labels and command comments:
`/opencode` on issues (the agent works the issue and opens a draft PR) and
`/distill` on discussions (the agent reads the fetched thread, distills
it into issues - plan / happy path / criteria of success - and closes the
discussion as resolved, ADR 0050). A trusted `/elaborate` discussion
comment instead kicks one session that fans out to the vendored persona
roster (domain expert, infra/architecture, marketer, seller, CTO -
`agent-config/agents/`) and posts one attributed POV comment per persona
back on the discussion - the comments are the deliverable (no branch, no
 PR). Runners reach the stack
API over the host gateway, allowed by the egress role's per-user dport
exceptions. No inbound listener on the VPS. The issue kick prompt mandates
the full pipeline (ADR 0036/0042): plan first - a draft refined by one
task subagent per persona in the shared `agent-config/agents/` roster
and posted as an issue comment, with each role's input or explicit
no-objection, before code - execution through task subagents, an
adversarial-review subagent pass on the diff before the ready gate, and
a verify step running the repo's declared gate (the
`veggies-verify-gate` marker in its agent-instruction file, ADR 0045),
not a hardcoded command; then the ready-gate (ADR 0046): green checks
and mergeable against current main - rebasing first - before
`gh pr ready` as the final act. Observability (ADR
0034): kicked sessions are titled `#N: <issue>` / `D#N: <discussion>`
(`D#N elaborate: <title>` for persona-roster runs), the
workflow comments the session link back on issues (discussions get a
minimal ack - discussions take GraphQL `addDiscussionComment`, issues
`addComment`, ADR 0039), and operators watch via `veggies ui`
(ssh tunnel helper) / `veggies sessions` / the web UI. Session listings
are live-first with idle history capped behind `--all` (ADR 0044). Session
isolation (ADR 0037): every kicked session works in its own git worktree at
`/workspace/.veggies/wt/issue-N` inside the shared clone (the kick prompt
mandates the bootstrap; `veggies up` excludes `.veggies/` via the clone's
`.git/info/exclude`), so parallel sessions never share a checkout. Freshness:
kicked sessions fetch and branch off `origin/main` per kick, while the
shared clone checkout (and the `veggies.yml` read at up time) only advances
on `veggies sync <name>` - pull plus re-up, the one-command
"merged-to-main -> live on the stack" path.

Supervision has two shapes (ADR 0028/0036): operator-driven
(`veggies supervise`, judges via `podman exec` into the litellm container)
and the opt-in always-on `supervision: supervisor` component, an in-pod
sidecar that judges every finish of a *kicked* session with a different
model over pod loopback and posts an async refinement below threshold.
Only sessions created after the daemon starts are judged, PASS/STOP are
log-only (a posted message would re-run the agent), and the router master
key never leaves the pod. Spend reporting is contract-pinned, writer
pending (ADR 0022/0051): `veggies costs` reads the per-call spend log
`<state_root>/<name>/spend.jsonl*` under the stack state root, but no
stack writes that file until the metering lands with issue #46 - until
then the command reports no spend log and exits 0.

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
                 plugins/, litellm/
cli/             the veggies CLI: veggies.py, veggies_stack.py,
                 capabilities.py, components/ (opencode, litellm, squid,
                 supervisor, mcp_toolbox)
deploy/          Containerfiles + component payloads (MCP toolbox server,
                 supervisor daemon)
scripts/         tfvars_from_vault.py, vault_get.py, stack_kick.py
tests/           pytest suite + machine-generated golden pod.yaml
docs/            architecture.md, runbook.md, threat-model.md, adr/
.github/workflows/  infra-ci.yml, agent-trigger.yml
```
