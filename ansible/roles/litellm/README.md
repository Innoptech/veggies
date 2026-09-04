# litellm

The LiteLLM model gateway - the only place provider API keys exist on the
host (ADR 0011). Runs rootless under the `egress-proxy` user (unrestricted
egress, ADR 0006), published on 127.0.0.1:4000 only.

## What it changes

- `~egress-proxy/.config/litellm/config.yaml` (model aliases, fallbacks) -
  content lives reviewably in `agent-config/litellm/config.yaml`.
- `~egress-proxy/.config/litellm/litellm.env` (0600, from the vault):
  `FIREWORKS_API_KEY` + `LITELLM_MASTER_KEY`.
- Quadlet `litellm.container`, image pinned (`ghcr.io/berriai/litellm:v1.99.1`).

## Adding a provider key

1. `mask vault-edit secrets/model.yml` - add the key.
2. Add the model block in `agent-config/litellm/config.yaml`.
3. Add the provider endpoint to `egress_model_endpoints` in group_vars.
4. `mask converge`. All in one PR.

## Be careful

- opencode and the runners authenticate to this proxy with virtual keys from
  `secrets/model.yml`; a leaked virtual key is revocable and only useful on
  127.0.0.1. The real provider key must never leave this container's env.
- No database in v1: spend tracking is in-memory/log-only. Postgres is a
  possible later addition (record it as an ADR if you add it).
