---
status: accepted
date: 2026-09-04
---

# 0019. Agent rosters and skills: convention over machinery

## Context

Workflows (ADR 0017) name agents; stacks need a workforce beyond the
built-ins. The open question was packaging: vendored files vs remote refs,
and how per-repo rosters layer over global defaults.

Verification (2026-09-04, opencode v1.18.27 docs + live stack) settled the
mechanism before any code was written: opencode already discovers
project-local agents and skills from the mounted repo. This ADR ratifies
that convention; the only work is documentation and trust rules.

## Verified substrate

- **Per-repo agents**: `.opencode/agents/<name>.md` in the workspace are
  discovered by the harness (verified live: a `demo-scout` agent dropped
  into a stack's repo appeared in `GET /agent`). Registration is
  **boot-time**: adding/removing a repo agent needs an opencode-container
  restart (or `veggies up`, which recreates). No volume pruning needed -
  the repo is bind-mounted, so edits and removals apply on restart
  (unlike the vendored global roster, whose bootstrap copy never prunes;
  see runbook §8).
- **Per-repo skills**: `.opencode/skills/<name>/SKILL.md` (plus `.claude/`
  and `.agents/` compat paths), same discovery rules.
- **Precedence** (opencode config merge order, later wins): remote <
  global (`~/.config/opencode`, where our vendored agent-config lands) <
  project (`opencode.json` / `.opencode/` in the repo). A repo agent named
  like a global one overrides it.
- **Roster introspection**: `GET /agent` returns the live merged roster -
  the orchestrator's drafter prompt and its load-time validation both
  consume exactly this (ADR 0017).
- `subagent_depth` (default 1) gates nested Task-tool fan-out; relevant if
  roster agents should themselves delegate.

## Decision

- **Three tiers, all conventions, zero CLI code**: built-ins (build/plan/
  general/explore/scout + hidden system agents) < vendored global
  (`agent-config/agents/`, baked per stack at bootstrap) < per-repo
  (`.opencode/agents/`, discovered from the mounted repo). Same pattern for
  skills. veggies.yml gains no roster keys - a stack's wiring and a repo's
  workforce are different files by design.
- **Versioning**: each tier versions with the repo it lives in (infra repo
  for global, target repo for per-repo). No remote refs, no digest pinning
  of agent files; boring git history is the audit trail.
- **Trust**: a mounted repo already gets bash inside the stack, so a repo
  roster's marginal power is prompt-shaping and per-agent permission
  overrides (project overrides global). Accepted: do not run stacks on
  repos you wouldn't hand a shell to. For sensitive repos the mitigation is
  a vendored global lockdown (`permission` in agent-config/opencode.json)
  plus review of `.opencode/` in PRs - it is config, reviewed like code.
- **Removal semantics** (verified): per-repo entries disappear on restart;
  global entries additionally need the stale copy pruned from the
  opencode-home volume (runbook §8).

## Consequences

- The drafter (ADR 0017) sees repo agents automatically via `GET /agent`;
  a repo can ship both its pipelines (`workflows/*.yaml`) and its
  workforce (`.opencode/agents/`) — the "super-AI team" per repo is two
  directories of markdown.
- New global agents/skills remain infra-repo PRs (runbook §8); new repo
  agents are ordinary repo PRs.
- ADR 0018 (MCPs) stays orthogonal; ADR 0017's roster dependency is
  satisfied by this convention.

## Deviation ledger

None - this ADR documents verified existing behavior plus policy.
