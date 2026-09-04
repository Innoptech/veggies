---
status: proposed
date: 2026-09-04
---

# 0020. Cloud substrate module (GCP / other)

> **Proposed, needs refining.** Context and open questions only; nothing here
> is decided. Do not implement against this ADR.

## Context

Stacks run "locally or on a VPS or a GCP VM". The CLI's `--host` abstraction
already makes any ssh-reachable box a target; what is missing is terraform
parity beyond OVH.

## Open questions

- One terraform module per cloud vs a generic "bring-your-own-VM" contract?
  The real interface a substrate must satisfy is small: tailscale + podman +
  the `stacks` user + egress policy hooks (ADR 0014, 0016).
- GCP shape if a module is built: e2 family, free-tier fit, image family
  (Fedora Cloud?), firewall parity with the OVH public-ingress + egress
  model (ADR 0006, 0008).
- Is this terraform at all, or an ansible inventory convention plus docs?
  (TODO(verify): whether GCP stays in the free tier for an always-on VM.)
- Naming: does a second host make inventory groups (e.g. `substrates`)
  worthwhile, and does the backup role stay per-host?
