---
status: accepted
date: 2026-09-11
---

# 0036. Always-on critic for kicked sessions: in-pod supervisor component

## Context and problem statement

Issue #26: an issue kicked through the ADR 0033 event path does not go
through the pipeline this project exists to run - a plan, subagent
execution, and adversarial review. Two separate gaps:

1. **The prompt was flat.** The kick template listed rules of engagement
   but never mandated the workflow, so unattended sessions optimized for
   "finish the task" and skipped straight to code.
2. **Nobody judged.** `veggies supervise` (0028) is operator-invoked; for
   kicked sessions there is no operator, so the adversarial gate existed
   only on paper. 0028 deferred an always-on mode as "a deliberate later
   step" - this is that step.

## Decision drivers

- The master key must never leave the pod (0028 invariant, built then as
  the podman-exec judge seam).
- No new inbound listeners, no always-on host daemons owned by hand
  (0024 posture; the CLI owns stacks, 0016).
- A refinement message RE-RUNS the agent (same `prompt_async` semantics
  the kick uses, verified 0033): the supervisor must never resurrect
  finished work, and must never post on a pass.
- Boring over clever: reuse the tested pure critic logic
  (`cli/supervisor.py`) and the component seam (0018/0023) instead of
  inventing a control plane (the 0025/0028 failure mode).

## Considered options

- **A. Prompt-only**: mandate the pipeline in the kick prompt, no new
  machinery. Cheap, but the adversarial gate stays a human chore.
- **B. In-pod supervisor component** (plus A): an opt-in sidecar polls
  the harness API on pod loopback, judges kicked sessions via the in-pod
  router, posts refinements.
- **C. Host-side systemd unit running `veggies supervise --all`**: needs
  the CLI + PyYAML + state.json + vault password on the VPS as the stacks
  user; remote state lives on the operator machine by design (0014). A
  GHA-side poller is impossible too: judging needs in-pod access, which
  the gh-runner user must not have.

## Decision outcome

**B plus A**, in one change set:

- The kick prompt (`scripts/stack_kick.py`) mandates the pipeline in
  order: brainstorming -> writing-plans with the plan posted as an issue
  comment before any code (a human can veto direction cheaply), execution
  through `task` subagents, an `adversarial-review` subagent pass on the
  diff before pushing, and verified claims only (`mask ci`).
- New opt-in capability `supervision` with a `supervisor` component
  (`supervision: supervisor` in veggies.yml; v0 `components:` can name it
  too). It ships the pytest-covered `cli/supervisor.py` verbatim plus a
  stdlib-only daemon (`deploy/supervisor/daemon.py`) as stack-config
  files, runs them in a pull-only `python:3.13-alpine` container, and
  reuses the harness serve password and router master key from their
  existing podman secrets - it declares no secrets, sets no proxy env,
  publishes no ports.
- **Scope guards** (the load-bearing details):
  - only sessions titled `#N: ...` (kicked, 0034) AND created after the
    daemon starts are judged - a pod recreate never re-judges the
    back-catalog;
  - PASS and STOP are **log-only**: any message posted to a session
    re-runs the agent, so a visible marker would loop forever;
  - judge garbage marks the finish judged and fails loud in the pod log -
    never a silent pass, never an infinite retry burning tokens;
  - a session that exhausts its refinements is left for a human (the log
    says so); re-kicking per 0035 is the recovery path.
- The operator-driven `veggies supervise` remains for interactive
  sessions and keeps the podman-exec judge path.

## Consequences

- Positive: the issue->agent loop now includes the adversarial gate with
  zero human steps and zero new long-lived host processes; the podman-exec
  seam is no longer on the kicked path; this repo's own stack dogfoods it.
- Negative/accepted: in-memory judgment state - a pod recreate forgets
  scores, which combined with the created-after-start guard means
  in-flight sessions at recreate time are simply never judged (operator
  runs `veggies supervise` by hand if one matters).
- Negative/accepted: the supervisor container holds the master key in
  env, like the litellm container - same pod, same secret store, same
  trust domain; the 0028 invariant is "never leaves the pod", kept.
- The judge model defaults to deepseek-v4 judging kimi-k3; stacks that
  re-point the author model must keep the two different. There is no
  per-stack tuning knob - the defaults live in the daemon and changing
  them is a PR, like everything else here.
- `veggies up` on a supervision stack pulls one more image and waits on
  the supervisor's heartbeat probe (first beat lands before any IO, so
  cold pods come up healthy).

## Links

- Amends the operator-invoked stance of
  [0028](0028-retire-canvas-own-the-critic-loop.md) and the "supervision
  stays operator-invoked" consequence of
  [0033](0033-issue-triggered-agent-kicks.md) (for kicked sessions only).
- Builds on the component seam ([0023](0023-capability-model-dependency-reversal.md)),
  the sidecar payload pattern ([0018](0018-mcp-server-components.md)),
  kicked-session titles ([0034](0034-session-observability.md)), and the
  re-kick semantics of [0035](0035-one-shot-labels-and-done-guard.md).
- Issue: Innoptech/veggies#26.
