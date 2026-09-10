---
status: accepted
date: 2026-09-04
---

# 0018. MCP server components

Accepted 2026-09-09 with the design below (the 2026-09-04 draft was
proposed-only; opencode MCP client capabilities verified against
opencode.ai/docs 2026-09-09).

## Context

Stacks should gain MCP servers as first-class, opt-in pod components -
"easily connect MCPs" must mean a one-line veggies.yml change, not image
surgery. opencode's MCP client supports `type: remote` servers over HTTP
with header/env-substituted credentials, and pod containers share one
network namespace, so a sidecar listening on pod loopback needs no
published port (0024 untouched).

## Decision

- **`mcps:` key in veggies.yml** (list of names, validated against
  `MCP_REGISTRY` in `cli/veggies_stack.py`; stored on `StackSpec.mcps`).
  Opt-in per stack; nothing changes when unselected.
- **Transport: Streamable HTTP on pod loopback** (`127.0.0.1:<port>`, one
  fixed port per MCP, never published to the host). Rejected alternative:
  stdio commands inside the opencode container - it bloats the harness
  image with every MCP's runtime (node/python/jvm), shares failure domains,
  and gives the MCP the harness's filesystem and secrets.
- **Wiring**: a component may implement `mcp_entry(ctx)` returning the
  opencode `mcp:` config fragment; the opencode component merges all
  non-None entries into the rendered opencode.json (dependency reversal per
  0023 - opencode never imports MCP modules).
- **Egress**: a component may implement `egress_domains(ctx)`; squid merges
  these into `allowlist.txt` (the ansible substrate list stays the outer
  boundary; the two mirrored base lists and their drift tests are
  unchanged).
- **Secrets**: per-MCP `SecretSpec(name_suffix=f"mcp-{name}")`, consumed
  via `secretKeyRef` env on the MCP container; opencode receives the same
  key and passes it as an `{env:}`-substituted header. Never literal values
  in opencode.json (it lands world-readable in stack-config).
- **Images**: tag+digest pinned like every component; supply-chain note
  owed to docs/threat-model.md per new image.
- **Reference implementation**: `toolbox` - zero egress, zero secrets, a
  tiny FastMCP server proving component → pod → opencode.json → tool call.
- **SonarQube** (the motivating case) is deliberately deferred: its MCP is
  a config-only add once a backend is chosen (shared ansible-managed server
  vs per-stack vs SonarCloud - undecided). Verified 2026-09-09: the
  official `sonarsource/sonarqube-mcp` image speaks Streamable HTTP
  (`SONARQUBE_TRANSPORT=http`), takes the token as a Bearer header, and the
  VPS has the RAM and `vm.max_map_count` a self-hosted server would need.

## Consequences

- New MCP = one file in `cli/components/` + one registry line (+ secret +
  egress domains if needed). No CLI core changes.
- The OpenHands native harness (0027) is NOT wired to MCPs yet: whether
  canvas agent-profiles accept `mcp_config` is TODO(verify) upstream. Probe
  first, follow-up ADR if yes.
- State: `mcps` persists in state.json (default `()`; older state files
  without the key keep working).
