---
status: proposed
date: 2026-09-04
---

# 0017. Agent orchestrator and workflows

> **Proposed, needs refining.** Context and open questions only; nothing here
> is decided. Do not implement against this ADR.

## Context

Stacks should grow from one opencode agent to multi-agent workflows:
plan -> delegate -> review loops with close to no human input, observable
via `veggies status` and attachable on demand.

## Open questions

- Orchestrator as a long-running pod component (service + queue) vs a
  CLI-driven step runner? A service survives detach naturally; a CLI runner
  is simpler but dies with the terminal unless itself containerized.
- Workflow definition format: `workflows:` in veggies.yml vs standalone
  files? Reuse an existing engine (boring-first) vs custom?
- Escalation policy: when the "no human input" goal breaks, what happens —
  pause-and-notify, attach-and-interrupt semantics, per-workflow approval
  gates?
- Build on opencode's native subagent support (TODO(verify): current
  capabilities/limits of opencode v1.x subagents) or an external engine?
- Queue substrate: sqlite (boring, matches host skills) vs none (in-memory)
  vs redis (extra component).

## Dependencies

- ADR 0018 (components must exist before something orchestrates them)
- ADR 0019 (rosters define the workers)
