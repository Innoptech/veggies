---
status: accepted
date: 2026-09-09
---

# 0027. Second harness: OpenHands-native conversations in the canvas component

## Context

ADR 0025 adopted Agent Canvas as the control plane, driving opencode via
ACP, with a recorded caveat: Canvas/SDK agent-loop features (critic +
iterative refinement, goal loops, lifecycle hooks, persistent memory) do
not cross ACP - they exist only for OpenHands-native conversations. We
want them natively (operator decision, 2026-09-09).

The canvas image already embeds an agent server; a "second harness" needs
no new container: an OpenHands-kind conversation runs the SDK's CodeAct
loop right there, needing only an LLM profile. ADR 0023 explicitly
anticipated a second harness.

## Decision

The canvas component doubles as the second harness:

- `bootstrap.py` additionally registers an LLM profile pointing at the
  stack's litellm (`http://127.0.0.1:4000/v1`, master key via the existing
  podman secret - same `secret_env` pattern as the opencode component; no
  new secrets are created).
- Agent kind is per-conversation in the Canvas UI: ACP/opencode remains
  the default (TUI/web-attach ecosystem, vendored rosters); OpenHands kind
  unlocks critic, goal loops, hooks, memory. No migration, no flag day.
- **Critic = LLM-as-judge via OUR litellm** (sovereign). The
  OpenHands-hosted trained critic was rejected: it would make
  all-hands.dev a second third-party data sink (Fireworks is the only one
  we accept today). If self-judging proves too weak in practice, revisit
  with an explicit data-boundary decision - not by default.
- Tools execute in the canvas container as container-root (= the stack
  user on the host): same ownership story as the harness, same shared
  /workspace mount, egress via the in-pod squid.
- Hooks (deterministic pre-stop checks) are a follow-up plugin, not part
  of this change.

## Consequences

- Positive: no new container, image, or port; one component grows an env
  var + bootstrap steps. Both agent kinds share one UI, one state volume,
  one model router. Self-correction (critic + refinement, goal
  completion) becomes configuration, not orchestrator machinery (0026).
- Negative / accepted trade-offs: the litellm master key enters Canvas's
  settings store (canvas-state) - same trust domain as the pod, but a
  canvas-state leak now includes model spend. Accepted: loopback-only
  service, 0600 files. The critic API is flagged experimental upstream;
  pin bumps may need bootstrap adjustments (our tests cover OUR render,
  not upstream behavior).

## Links

- ADR 0023 (capability model: second harness anticipated)
- ADR 0025 (control plane; the ACP caveat this resolves)
- ADR 0026 (orchestrator retirement; this completes the self-correction story)
