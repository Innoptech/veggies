---
status: proposed
date: 2026-09-04
---

# 0022. Cost metering and model routing

> **Proposed, needs refining.** Context and open questions only; nothing here
> is decided. Do not implement against this ADR.

## Context

At multi-agent scale (ADR 0017), spend control matters: per-agent
attribution, budget caps, and fallback model chains. **Known conflict**:
ADR 0011 dropped litellm's DATABASE_URL because litellm removed sqlite
support — and litellm's spend tracking historically lives in that database.
Metering therefore needs a new substrate decision first.

## Open questions

- Metering substrate: reintroduce a DB (postgres as an optional pod
  component — TODO(verify): current litellm spend-tracking backends and
  whether postgres is still the supported path), or log-based export
  (litellm success/failure logs -> host file -> backup)?
- Per-agent attribution: one litellm virtual key per agent (ADR 0019
  rosters) vs per stack (today)? Key count vs granularity tradeoff.
- Budget enforcement: litellm `max_budget` per key vs CLI-side pre-flight
  checks vs both.
- Fallback chains: where is routing policy declared — veggies.yml `models:`
  section vs litellm config rendered by the CLI from secrets/model.yml?
- Reporting surface: `veggies status` cost lines vs a separate
  `veggies costs` subcommand?
