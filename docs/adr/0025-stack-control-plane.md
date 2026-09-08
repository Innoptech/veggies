---
status: proposed
date: 2026-09-08
---

# 0025. Stack control plane: supervision UI and automations

## Context and problem statement

Stacks are operable today through the TUI (`opencode attach`) and the CLI
(`status`, `runs`, `logs`). The target experience is richer: watch several
agents at once in a browser, message or reroute them mid-run, review diffs,
and fire automations (schedule/webhook -> agent). Verified 2026-09-08:

- `opencode serve` already serves the official web UI on the stack's
  published port: sessions list, live multi-session view, permissions -
  one browser tab per stack, via `ssh -L` (ADR 0024). No code of ours.
- ADR 0017 rejected OpenHands as "a competing full platform" *for the
  harness/orchestrator layer*. That rejection stands - but OpenHands has
  since pivoted to **Agent Canvas**, a self-hosted control center that
  drives *any ACP-compatible agent* (Claude Code, Codex, Gemini), and
  opencode is ACP-compatible (`opencode acp`, all features but /undo).
  Canvas supports arbitrary **custom ACP launch commands** (Settings ->
  Agent -> Custom), so `podman exec -i <opencode-ctr> opencode acp` is a
  legitimate integration, and it ships automations (cron/webhook) plus
  multi-backend supervision.
- Vibe Kanban (the other known "kanban of coding agents") is officially
  sunsetting - ruled out.
- No ADR has ever covered the control-plane/UI layer; it is unowned ground.

## Decision drivers

- Do not rebuild what exists (boring over clever, AGENTS.md rule 7).
- Keep opencode as the harness (ADR 0017/0023 are unchanged by this).
- Keep the security posture: nothing new on a public interface (ADR 0024);
  UI behind 127.0.0.1 + `ssh -L` until the tailnet exists.
- Evidence over opinion: a timeboxed spike with a written scorecard decides.

## Considered options

- **A. opencode web only** (+ `veggies ls/status`): per-stack browser UI we
  already have; no cross-stack view, no automations, no diff-centric review.
- **B. OpenHands Agent Canvas** as control plane, driving stack opencode via
  ACP (`podman exec -i ... opencode acp`), run as the `stacks` user with its
  UI on 127.0.0.1. If adopted permanently it becomes *substrate* (an ansible
  role, ADR 0016) - it supervises stacks, it is not part of one.
- **C. Thin own dashboard**: a small read-mostly webapp over
  `~/.local/state/veggies/state.json` + the serve APIs. Full control; we own
  a webapp forever.

## Decision outcome

**Decide by spike.** A half-day spike of Option B on the VPS, then this ADR
is finalized (accepted with the winner) using this scorecard:

1. Two+ sessions visible and followable in one UI.
2. Message a running agent mid-run (reroute) and get a response.
3. Permission prompts surfaced and answerable from the UI.
4. Diff review usable enough to gate a merge.
5. One automation runs (schedule or webhook).
6. ACP-via-`podman exec` survives the pod topology (restart, multiple
   stacks) without hacks.
7. Footprint acceptable next to stacks on the VPS (RAM/CPU measured).
8. Clear delta over Option A; otherwise A wins by boring-first.

Default if the spike is inconclusive: **Option A** (we already have it).
Option C is only reconsidered if A proves insufficient for daily driving.

## Consequences

- Positive: the UI question gets an evidence-based answer in days, not a
  framework marriage; if B passes, automations (ADR 0017's escalation
  channels, ADR 0022 triggers) arrive for free.
- Negative / accepted trade-offs: the spike installs Node.js + uv +
  `@openhands/agent-canvas` on the VPS host (substrate mutation done by
  hand; codified into ansible only if adopted). ACP sessions may not appear
  in the serve server's session list (separate process, shared storage -
  the spike checks). Webhook-triggered automations would need an inbound
  public endpoint - NOT part of the spike; a separate decision if B is
  adopted.

## Links

- ADR 0017 (orchestrator; OpenHands rejection for the *runtime* layer)
- ADR 0023 (capability model - the harness contract ACP plugs into)
- ADR 0024 (interim access posture: 127.0.0.1 + ssh -L)
- https://docs.openhands.dev/openhands/usage/agent-canvas/acp-agents
- https://opencode.ai/docs/acp/ and /docs/web/
