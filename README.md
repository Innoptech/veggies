# infra

Infrastructure-as-code and configuration management for **veggies**: an
always-on OVHcloud VPS hosting autonomous coding agents (opencode) and
ephemeral self-hosted GitHub Actions runners, reachable over hardened,
key-only SSH (Tailscale-only access is deferred - ADR 0024). Know what a
pull request costs, not what an API call costs - `veggies costs` reads a
spend log that never leaves the host (ADR 0022).
Everything about the machine - access, secrets, network policy, merge policy -
is reviewable code in this repo.
Label an issue `agent-task` and the repo's own agent stack works it in the
open on a draft PR - commits and CI land there as it goes - and marks it
ready only when it is green and merges cleanly against main (ADR 0046).
When a draft goes ready, a different model posts a comment-only,
risk-ranked audit of the diff against the issue's acceptance criteria -
the approval and the merge stay human (ADR 0054).
Approved PRs then merge through GitHub's native merge queue (ADR 0053) - the
agents out-produce their reviewer, so the queue, not the operator, owns the
update-branch ritual.

## Quickstart

```bash
git clone <this-repo> && cd veggie   # repo dir name may differ; this is the repo root
mask setup                           # venv + pinned tools + pre-commit hooks
$EDITOR ~/.config/infra/vault-password && chmod 600 ~/.config/infra/vault-password
# ^ one line, required for `veggies up` - ask the operator for the password (out-of-band)
```

Tool versions are pinned in `requirements-dev.txt` and the tool tarballs
(`mask setup` installs everything).

## Stacks: the daily driver

One stack = one repo = one rootless pod (opencode + litellm + squid),
persistent, locally or on the VPS:

```bash
mask veggies-install      # once: puts `veggies` in ~/.local/bin
cd ~/code/some-repo
veggies up                # prompts, builds, starts, attaches
veggies ls                # all stacks, live status
veggies attach <name>     # back into a running stack
veggies down <name>       # stop (keeps state); --purge deletes everything
```

A repo can declare its stack in a `veggies.yml` at its root, versioned with
the repo:

```yaml
model: kimi-k3            # litellm alias; becomes the stack's default model
harness: opencode         # which implementation provides each capability
model_router: litellm
egress: squid
# github: true            # opt-in: GH_TOKEN + gh in the pod for pushes/PRs (ADR 0030)
```

A repo keeps its own conventions: CLAUDE.md/AGENTS.md and
`.claude/`/`.opencode/` agents and skills are discovered as-is, zero
config - installing a stack never means converting the repo (ADR 0019/0049).

## Docs

| Doc | What |
|-----|------|
| [docs/architecture.md](docs/architecture.md) | the system as it is today + repo layout |
| [docs/runbook.md](docs/runbook.md) | operations: rebuild, secrets, stacks, workflows, troubleshooting |
| [docs/adr/](docs/adr/README.md) | decisions, and the deviation ledger vs the original brief |
| [docs/threat-model.md](docs/threat-model.md) | threat model |
| [AGENTS.md](AGENTS.md) | rules for coding agents working here - agents read this first |

Conventions: `TODO(you)` = human supplies the value; `TODO(verify)` =
uncertain upstream detail, check docs before relying on it. Commits are
conventional, small, single-purpose.
