---
status: accepted (amended by 0036)
date: 2026-09-09
---

# 0028. Retire the canvas control plane; own the critic loop

Supersedes 0025 (canvas component) and 0027 (second harness in canvas).

## Context

Three findings, all verified 2026-09-09:

- **Upstream is dead**: `OpenHands/agent-canvas` is archived (read-only).
  The whole harness-agnostic control-plane niche collapsed this year
  (vibe-kanban's company shut down April 2026 - "no business model",
  Crystal deprecated, Conductor gone): harnesses absorbed supervision
  (opencode's web UI) from below and platforms (GitHub, Open SWE) from
  above.
- **The two-worlds cost is real**: canvas conversations and opencode
  sessions are separate stores; users must know which harness started the
  work to find it. Canvas's MCP path is closed (agent-profiles 422 on
  `mcp_config`, probed), so the tooling we build (0018) only reaches
  opencode anyway.
- **The valuable piece was always ours**: the critic's judge logic
  (rubric, prompt, parsing) is our shim; what the SDK added was a hook
  (FinishAction), a renderer (built for their trained classifier; pure
  overhead for an LLM judge), and message injection. opencode serve
  exposes equivalents (`/session/status`, `/session/{id}/message`), and
  the critic currently judges only canvas conversations - the main
  harness, where most work happens, has no critic at all.

## Decision

- **Drop the canvas component entirely** (container, image, bootstrap,
  shim adapter, `canvas:` veggies.yml key, `StackSpec.runtime_dir`).
  0025's spine constraint (CLI owns stack definition, opt-in components)
  is unaffected; the control-plane capability simply has no
  implementation now.
- **Own the loop**: `veggies supervise <stack> --session <id>` watches an
  opencode session (poll status until idle with an unjudged assistant
  finish), renders the transcript, judges it with a *different* model
  than the author via the in-pod router (default deepseek-v4 judging
  kimi-k3), and posts a visible refinement message when below threshold
  (default 0.6, max 2 iterations). The judge call executes inside the
  litellm container via `podman exec` with the request on stdin - the
  master key never leaves the container and is never argv/printed.
- **Operator-invoked, on purpose**: supervision runs while invoked from
  the CLI; an always-on mode (systemd unit or the opencode-scheduler
  plugin) is a deliberate later step, not an MVP gap.
- Deliberately lost: the OpenHands-native second harness (execution
  diversity), goal loops/scheduled conversations (unused), and the
  agent-canvas UI. opencode's web UI + this supervisor cover the daily
  and the critic'd workflows respectively.

## Consequences

- One harness, one session store, one UI to learn: the two-worlds problem
  and the "which harness did I start it in?" question disappear.
- The critic now covers where work actually happens (opencode sessions).
- `veggies.yml` `canvas:` key is removed from the schema; older state
  files with a `control-plane` selection are migrated on load (dropped
  with a warning).
- If a maintained, self-hostable supervisor-with-API ever re-emerges, the
  opt-in component seam (0023/0025) is still there to receive it.
