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

## Verified substrate (opencode v1.18.27, live-tested on veggies-smoke 2026-09-04)

Every primitive below was exercised against a real stack with a real model,
not just read from docs:

- **Sessions**: `POST /session` (optionally `parentID`), `DELETE` cleans up.
- **Fork**: `POST /session/:id/fork` at a `messageID` — the "parallel forks"
  primitive. Forks run independently with independent `/diff`s. Caveat
  (verified): forks get `parentID: null` and do NOT appear in
  `/session/:id/children` — that endpoint tracks only Task-tool subagents
  (verified: subagent sessions get a real `parentID` and are listed). The
  orchestrator must persist fork IDs itself (they are the fork call's
  return values).
- **Dispatch**: `POST /session/:id/prompt_async` with per-call `agent` +
  `model`. Caveat (verified): an unknown `agent` name returns success and
  silently does nothing — the orchestrator must validate roster names
  against `GET /agent` before dispatch and treat mismatch as a load error.
- **Concurrency (verified)**: 3 async sessions were simultaneously
  `{"type": "busy"}` on one server and all completed. Higher ceilings
  unmeasured; the orchestrator takes a configurable max-parallel knob
  regardless.
- **SSE taxonomy (verified)**: `session.created/updated/status/idle/diff`,
  `message.updated`, `message.part.updated/delta`, `permission.asked/
  replied`, `server.connected/heartbeat`. `session.idle` marks completion;
  `/session/status` lists only non-idle sessions (absence = idle).
- **Headless permissions (verified, both paths)**: an agent with
  `permission: {edit: ask}` attempting a write pauses with a
  `permission.asked` SSE event (`id`, `sessionID`, `tool`, `patterns`,
  `always`); `POST /session/:id/permissions/:permissionID` with
  `{response: "once"}` resumes and the file lands; `"reject"` blocks it.
  Escalation v1 is exactly this plus a status probe.
- **`opencode run` (verified flags)**: `--agent`, `--model`,
  `--session/--continue/--fork`, `--attach <server-url>` with
  `--username/--password`, `--format json` (raw event stream), `--dir`.
  Headless one-shots against a stack need no local server.
- **Config is bootstrap-time (verified)**: `PATCH /config` is a
  non-persistent echo (a runtime-added agent never registered; a patched
  permission did not stick). Roster/config changes ship via agent-config +
  container restart; agent *removal* additionally needs the stale file
  deleted from the opencode-home volume (`cp -r` doesn't prune) — noted in
  the runbook.

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
notify_webhook: https://...          # optional, escalation v1
steps:
  - id: plan
    kind: agent                      # agent | shell
    agent: plan                      # roster name (ADR 0019)
    model: kimi-k3                   # optional; default = stack model
    prompt: "Plan the change: {{ task }}"
    timeout: 30m                     # optional, this is the default
    required: true                   # the planner may not skip this
  - id: implement
    kind: agent
    agent: build
    needs: [plan]
    parallel: 3                      # fork the session N ways
    combine: best-of                 # best-of (reviewer picks a diff) | shard
    prompt: "Implement: {{ steps.plan.output }}"
  - id: check
    kind: shell                      # non-agent gate; exit code = pass/fail
    run: "pytest -q"
    needs: [implement]
  - id: review
    kind: agent
    agent: review
    needs: [check]
    gate: approval                   # pauses for a human (see escalation)
    prompt: "Review the diff: {{ steps.implement.diff }}"
```

Schema v0 rules (decided 2026-09-04):

- `kind: agent | shell`. Shell steps run via `POST /session/:id/shell`
  (harness-managed, audited alongside agent work); their exit code is the
  gate. A failing shell step fails the run unless the planner declared a
  recovery step.
- `parallel` + `combine`: `best-of` = N forked sessions attempt the same
  task, a downstream agent step picks the winning diff (costs N× tokens,
  for hard problems); `shard` = the planner splits the task into N
  non-overlapping subtasks, results concatenate. Absent = single session.
- Prompts are **Jinja2** (same engine as Ansible) rendered strict-undefined:
  an unknown placeholder fails at load, never mid-run. Documented names:
  `task`, `steps.<id>.output`, `steps.<id>.diff`. Logic in prompts is
  possible but discouraged in schema docs — branching belongs to the
  planner, not templates.
- `timeout` per step (default 30m): a hung agent fails the step, never
  hangs the pipeline. **No `retries` in v0** — retries hide model
  flakiness; add them when evidence says they're needed.
- Validation fails fast: unknown keys, unknown roster agents (checked
  against `GET /agent` — the API itself won't complain), and
  `needs`-cycles are load errors. Unlike veggies.yml, nothing warns;
  pipelines run unattended.

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

An opencode `ask` permission (verified flow: `permission.asked` SSE ->
answer `once`/`reject` via the API) or a `gate: approval` step pauses the
pipeline; the orchestrator's status probe reports `awaiting-approval`
(with step id and reason); if the workflow declares `notify_webhook`, it
POSTs once per escalation. The human either attaches
(`veggies attach <stack>`) and answers interactively, or answers the
permission via the API. No louder channels in v1.

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

For genuinely one-shot jobs the planner's collapse rule applies (same
pipeline def, one step); mechanically that's one `opencode run --attach
http://127.0.0.1:<port> --agent <roster> --format json` inside the
ephemeral pod (flags verified 2026-09-04) — no separate CI code path.

Secrets flow over the existing vault-stdin path; the runner never sees
plaintext. This keeps the ADR 0016 consequence: no host-global litellm
returns; `LITELLM_API_BASE` stays deleted. `veggies run` and `--ttl` are new
CLI surface introduced by this ADR (implementation phase).

## Consequences

- `cli/components/orchestrator.py` becomes the fourth component; the registry
  line flips from `{}` to `{"orchestrator": <impl>}` and veggies.yml gains a
  working `orchestrator:` key.
- The orchestrator carries two verified-API obligations: validate roster
  names against `GET /agent` at load (silent no-op otherwise), and persist
  fork IDs itself (forks don't appear in `/children`).
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
