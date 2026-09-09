---
status: accepted
date: 2026-09-08
---

# 0026. Retire the orchestrator; supervision and automations move to the control plane

Supersedes: 0017

## Context and problem statement

ADR 0017 built a thin per-stack orchestrator (workflows over the harness
API: DAG steps, shell gates, fan-out, approval gates, sqlite run store)
because no control plane existed. Two facts changed on 2026-09-08:

1. opencode's `serve` port doubles as the official web UI (verified live):
   sessions list, multi-session view, permissions - no code of ours.
2. OpenHands Agent Canvas was spiked successfully as a control plane
   driving in-pod opencode via ACP (ADR 0025): multi-conversation
   supervision, mid-run messaging, permission prompts, diff review, and an
   automations service (cron + git-synced definitions; event/webhook
   triggers once the access posture allows inbound listeners - ADR 0024).

Meanwhile the orchestrator's own roadmap (fix-loops, parallel tiers,
durable approvals, an events surface) was accreting toward a hand-rolled
agent framework - the thing ADR 0017 explicitly refused to be. And its
differentiators are narrower than hoped: the harness's agent loop already
self-corrects in-session; rosters and skills carry conventions (0019);
the hard merge gate belongs to CI (0005/0007), not in-pod machinery.

## Decision

Retire the orchestrator: component, payload (`deploy/orchestrator/`),
image, CLI subcommands (`run`/`runs`/`approve`/`abort`), workflow schema,
and the cross-stack meta-stack sketch are removed. ADR 0017 is superseded
in full.

- Supervision + human-in-the-loop: Agent Canvas (0025) and the opencode
  web UI / TUI per stack.
- Triggers and scheduled work: Canvas automations (git-synced; event
  triggers stay off while inbound listeners are out of posture).
- Quality conventions ("run the checks, then adversarial review, then
  report to the human"): encoded in the vendored agents/skills (0019),
  enforced for merging by CI required checks (0007 + self-hosted runner,
  0005).
- Self-correction: the harness's agent loop. Canvas-side critic/goal-loop
  features are SDK-loop facilities that do not apply to ACP-delegated
  conversations; if they are ever wanted natively, the upgrade path is a
  second harness component (0023), not a rebuilt orchestrator.

## Consequences

- Positive: deletes ~900 lines of executor/schema surface and all of its
  future maintenance; one mental model for "watch/steer" (Canvas) and one
  for "conventions" (skills); no framework left to accrete.
- Negative / accepted trade-offs: headless YAML pipelines with real
  exit-code shell gates and fan-out (best-of/shard) are gone; nothing
  in-pod gates agent work deterministically anymore (CI is the gate); the
  drafter goes with it. No user had run a CI or cross-stack pipeline yet,
  so no working workflow migrates.
- ADR 0017 stays in the tree as history; its verified headless-harness
  findings (question-tool parking, dispatch no-ops, SSE taxonomy) remain
  cited in code where they still apply.

## Links

- Supersedes ADR 0017 (agent orchestrator and workflows)
- ADR 0025 (control plane: Agent Canvas via ACP)
- ADR 0019 (convention over machinery), ADR 0023 (capability model)
- ADR 0005/0007 (CI as the hard gate)
