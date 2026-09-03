---
status: accepted (target architecture; implementation deferred - see ADR 0008)
date: 2026-09-03
---

# 0002. OVH Public Cloud over the VPS product line

## Context and problem statement

The agent host must be rebuildable from zero by running code. OVH sells both
"Public Cloud" instances (OpenStack) and a "VPS" product line; they differ
completely in API surface.

## Decision drivers

- Idempotent, reviewable lifecycle: instance, security groups, volumes,
  snapshots, cloud-init.
- In-place resize without reprovisioning.
- No one-shot ordering APIs in the steady state.

## Considered options

- OVH Public Cloud via the `openstack` provider
- OVH VPS via the `ovh` provider (`ovh_vps` ordering resource)
- Manual VPS provisioning (what actually happened - ADR 0008)

## Decision outcome

Public Cloud, via `terraform/ovh/` on the `openstack` provider. The VPS path
was rejected because the `ovh_vps` ordering resource is one-shot and reported
brittle: it models *buying*, not *operating*.

## Consequences

- Positive: mature provider, idempotent resources, security groups model the
  "temporary SSH, then close" bootstrap as a variable flip (`admin_cidr`).
- Negative: OpenStack auth setup (clouds.yaml) is heavier than a single API
  token.
- Deferred: the module is a gated scaffold until the migration (ADR 0008).

## Pros and cons of the options

### Public Cloud (openstack provider)

- Good: idempotent instance/SG/volume/cloud-init; in-place resize; the
  provider is signed and registry-verified (v3.4.0).
- Bad: more concepts (projects, catalogs, flavor names per region).

### VPS (ovh provider)

- Good: cheaper; simpler mental model.
- Bad: ordering resource is one-shot; rebuilds are not convergent; security
  groups do not exist on VPS (only the optional OVH edge firewall).

### Manual VPS

- Good: instant.
- Bad: not code - acceptable only as the documented interim (ADR 0008).

## Links

- [terraform/ovh/README.md](../../terraform/ovh/README.md)
- ADR 0008 (interim platform)
