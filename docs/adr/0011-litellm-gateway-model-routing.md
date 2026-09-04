---
status: accepted
date: 2026-09-03
---

# 0011. LiteLLM gateway as the model router

## Context and problem statement

Agents should run on different models depending on the task (kimi / deepseek /
glm / ...), API keys must be trivially addable, and runner containers - which
process untrusted input - must never hold real provider keys.

## Decision drivers

- One place that holds provider credentials.
- Revocable, budget-cappable credentials for agents.
- Retries/fallbacks without touching opencode config.
- Chosen by the operator over the opencode-native alternative.

## Considered options

- LiteLLM proxy as a rootless quadlet (chosen)
- opencode-native per-agent model pins only
- Direct provider keys in opencode auth.json

## Decision outcome

A `litellm` quadlet under the `egress-proxy` user (unrestricted egress, see
ADR 0006), published on 127.0.0.1:4000. The Fireworks key lives ONLY in that
container's env file (from the vault). opencode and runner containers
authenticate with virtual keys (also vaulted); a leaked virtual key is
revocable and useless off-host. Task routing stays declarative in
`agent-config/agents/*.md` frontmatter (`model: litellm/<alias>`); LiteLLM
adds retries and fallbacks per alias. Image pinned
(`ghcr.io/berriai/litellm:v1.99.1`, tag existence verified).

## Consequences

- Positive: single credential store; revocable agent keys; fallback routing;
  one audited hop for all model traffic.
- Negative: one more service to run and back up; v1 has no spend DB
  (in-memory only) - a Postgres-backed deployment is a possible future ADR.
- Egress allowlist only needs `api.fireworks.ai` for now (Fireworks-only
  accounts); more providers = one PR (key + config + endpoint).

## Pros and cons of the options

### LiteLLM gateway

- Good: all of the drivers above; runner containers can be hard-blocked from
  provider hosts because they only need 127.0.0.1:4000.
- Bad: extra moving part; config format to learn.

### opencode-native pins

- Good: zero services; model per agent in frontmatter.
- Bad: provider keys land in opencode's auth store (reachable by the agent
  process); no fallbacks; no scoping per consumer.

### Direct keys

- Bad on every driver here.

## Links

- ansible/roles/litellm, agent-config/litellm/config.yaml, ADR 0006
