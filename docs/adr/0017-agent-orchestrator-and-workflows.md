---
status: proposed
date: 2026-09-04
---

# 0017. Agent orchestrator and adaptive pipelines

> **Proposed, under review.** The verified substrate and the rejected
> alternatives are settled inputs to the discussion; the Decision section is
> the proposal on the table. Do not implement against this ADR yet.

## Context

Stacks today run exactly one opencode harness, driven interactively by a
human. The goal: plan -> delegate -> review pipelines with close to no human
input — including parallel, dedicated agents working in forks of a session
(the claude-code-style fan-out) — observable via `veggies status`, attachable
on demand, and reusable as CI jobs on ephemeral PR stacks.

This ADR was rewritten after the ADR 0023 capability model landed: the
orchestrator is no longer an architectural open question, it is a component
slot. The registry already reserves `orchestrator` with zero implementations;
this ADR defines what the first implementation must do.

## Verified substrate (opencode server API, docs checked 2026-09-04)

The opencode server API (our harness, `opencode serve`, basic auth) already
exposes every primitive an orchestrator needs:

- `POST /session` with `parentID` — programmatic child sessions
  (dedicated agents).
- `POST /session/:id/fork` with `messageID?` — fork a session at a specific
  message. This is the "parallel forks" primitive: N agents branch off the
  same context and diverge.
- `POST /session/:id/prompt_async` (204, non-blocking) with per-call `agent`
  and `model` — dispatch work to a named roster agent on a chosen model
  alias, in parallel across sessions.
- `GET /event` (SSE stream), `GET /session/status`,
  `GET /session/:id/children` — the orchestrator's event loop and
  bookkeeping.
- `POST /session/:id/permissions/:permissionID` — approval gates are
  API-drivable; escalation does not require a TUI.
- `GET /session/:id/diff` — a review step sees what an agent actually
  changed, not what it claims.

Native tier (no orchestrator needed): primary agents already fan out to
subagents via the Task tool in parallel child sessions; `permission.task`
glob-gates which subagents an agent may invoke; rosters are markdown files
we already ship into every stack (ADR 0012/0019).

Still TODO(verify) before implementation:

- concurrency semantics: how many sessions may run in parallel on one
  `opencode serve` instance, and whether per-session state is isolated.
- the SSE event taxonomy (what marks a session step finished/failed).
- `opencode run` headless CLI flags (candidate for the CI one-shot path).
- `ask` permissions in a headless server: do they queue until answered via
  the API (assumed yes), or auto-deny?

## Engine survey (settled: rejected alternatives)

- **CrewAI** — rejected. It is an agent *runtime*, not a workflow engine over
  an existing harness: its own agent loop, tools, and crews would replace
  opencode inside the pod, costing us attach/TUI, the permission system, and
  the shipped rosters. (It routes models through litellm internally, so it
  would even bypass our harness while keeping our router.) If CrewAI-style
  role crews are ever wanted, they enter as a *harness* implementation in
  the registry (ADR 0023), never as the orchestrator.
- **LangGraph / AutoGen** — rejected for the same reason: frameworks for
  building agent loops, duplicating what opencode already is.
- **Temporal** — rejected: a durable-execution service is infrastructure
  weight that violates boring-first; our durability needs fit in sqlite.
- **OpenHands** — rejected: a competing full platform.
- **Keep:** opencode-native Task parallelism (in-session fan-out) plus a
  thin custom orchestrator driving the server API (workflow-level fan-out).
  The orchestrator is a state machine and a queue, not an agent framework.

## Decision (proposed)

### Topology: per-stack component, cross-stack by roster — one design

The orchestrator is a pod component: `provides="orchestrator"`,
`requires=("harness",)`, image/Containerfile owned via its BuildSpec,
secrets declared via SecretSpec, health via probes() — ADR 0023 end to end.
It drives its own stack's harness over `http://127.0.0.1:<port>`.

Cross-stack orchestration is not a second architecture: the orchestrator's
*roster* (from `~/.local/state/veggies/state.json`, which already records
every stack's endpoint + password) may include sibling stacks' harness APIs.
A future "meta" stack whose job is orchestrating other stacks is simply a
stack whose orchestrator's roster is mostly other stacks — an evolution of
this design, not a redesign.

### Workflows live in the target repo, not in veggies.yml

`workflows/*.yaml` in the repo the stack runs against (ADR 0013: repo-scoped
config belongs with the repo). veggies.yml stays pure stack wiring
(ADR 0023). Sketch:

```yaml
# workflows/feature.yaml
name: feature
notify_webhook: https://...        # optional, escalation v1
steps:
  - id: plan
    agent: plan                    # roster name (ADR 0019)
    model: kimi-k3                 # optional; default = stack model
    prompt: "Plan the change: {task}"
    required: true                 # the planner may not skip this
  - id: implement
    agent: build
    needs: [plan]
    prompt: "Implement: {plan.output}"
    parallel: 3                    # optional: N forked sessions, best-of/merge
  - id: review
    agent: review
    needs: [implement]
    gate: approval                 # pauses for a human (see escalation)
    prompt: "Review the diff: {implement.diff}"
```

### Adaptive execution: one pipeline def, the planner chooses the run

Every run starts with a **planner session** (a roster `plan` agent on a
cheap model alias) that receives the full pipeline definition plus the task
and returns, as structured output: the subset of steps to execute, the
order, parallelization, and **a written rationale per included/excluded
step**. Hard constraints the planner cannot override: `required: true`
steps and `gate: approval` steps stay in. The rationale is persisted in the
orchestrator's sqlite queue and surfaced by `veggies status` (probe) and in
CI logs. A trivial task collapses to a one-step run — which is also what CI
uses for simple jobs: same pipeline def, planner-selected single step, no
separate code path.

### Escalation v1

An opencode `ask` permission or a `gate: approval` step pauses the pipeline;
the orchestrator's status probe reports `awaiting-approval` (with step id
and reason); if the workflow declares `notify_webhook`, it POSTs once per
escalation. The human either attaches (`veggies attach <stack>`) and answers
interactively, or answers the permission via the API. No louder channels in
v1.

### State and queue

sqlite in the stack's state dir (`~/.local/state/veggies/<name>/`), covered
by the ADR 0021 backup story. No redis, no external service.

### CI: ephemeral stacks, same pipeline

The github_runner job for `/opencode`-style PR workflows becomes:

```
veggies up --clone <pr-url-ref> --name pr-<n> --ttl 2h --no-attach -y
veggies run pr-<n> workflows/ci.yaml --task "<pr context>"   # planner decides
# teardown by TTL, or explicit `veggies down pr-<n>` in an always-step
```

Secrets flow over the existing vault-stdin path; the runner never sees
plaintext. This keeps the ADR 0016 consequence: no host-global litellm
returns; `LITELLM_API_BASE` stays deleted. `veggies run` and `--ttl` are new
CLI surface introduced by this ADR (implementation phase).

## Consequences

- `cli/components/orchestrator.py` becomes the fourth component; the registry
  line flips from `{}` to `{"orchestrator": <impl>}` and veggies.yml gains a
  working `orchestrator:` key.
- `veggies status` needs no changes: the orchestrator publishes its queue
  state through probes() like every other component.
- New CLI verbs (`run`, `--ttl`) and the workflow schema get their own tests;
  the golden file gains a fourth container only in stacks that select the
  orchestrator (default selection unchanged: it stays opt-in).

## Dependencies

- ADR 0019 (rosters): the planner and the `agent:` fields name roster
  entries; rosters define the workforce. Hard dependency.
- ADR 0018 (MCPs): orthogonal — MCPs are tools the harness uses, not
  workers the orchestrator manages. No dependency.
- ADR 0021 (backup): covers the orchestrator's sqlite by construction.
- ADR 0022 (cost): per-step model aliases in pipeline defs are the metering
  hook when that ADR lands.
