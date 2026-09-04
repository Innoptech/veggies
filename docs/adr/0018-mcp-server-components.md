---
status: proposed
date: 2026-09-04
---

# 0018. MCP server components

> **Proposed, needs refining.** Context and open questions only; nothing here
> is decided. Do not implement against this ADR.

## Context

Stacks gain MCP servers as first-class pod components, declared via
`mcps:` in veggies.yml (schema v0 already treats `components` as a list so
this plugs in per ADR 0016).

## Open questions

- Transport: stdio sidecar inside the pod vs one HTTP server per component?
  (TODO(verify): which transports opencode's MCP client supports today.)
- Egress policy per MCP: squid rules are per-stack today; do MCPs need
  per-component allow lists, and how are they rendered into the shared
  squid.conf?
- Secret naming extension: `veggies-<stack>-mcp-<name>-<key>`; rotation and
  `veggies secrets` UX for many MCPs.
- Third-party MCP image provenance: pinning policy (digest-pinned like
  opencode), supply-chain row owed to docs/threat-model.md.
- Which MCPs ship in the default roster (filesystem? git? web?) and which
  stay opt-in per repo.
