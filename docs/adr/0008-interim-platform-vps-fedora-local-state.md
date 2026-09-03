---
status: accepted
date: 2026-09-03
---

# 0008. Interim platform: manually-rented VPS, Fedora 44, local state

## Context and problem statement

The brief specified Ubuntu 24.04 LTS on an OVH Public Cloud instance created
by OpenTofu, with remote S3 state. Before this repo existed, a VPS was
ordered by hand: 6 vCPU / 12 GB, **Fedora 44**, reachable as `garden`. The
code must target what exists without losing the recorded target
architecture (ADR 0002).

## Decision drivers

- The server already exists and costs money; value it, don't re-order.
- Keep every interim concession explicit and reversible.
- Do not fake provider coverage: classic OVH VPS has no security groups.

## Considered options

- Adopt the VPS as the platform (with the compute module as scaffold)
- Rebuild immediately on Public Cloud and discard the VPS

## Decision outcome

Adopt the VPS. Consequences ripple through the brief's table and are recorded
here once instead of scattered:

1. **OS: Fedora 44, not Ubuntu 24.04.** `dnf`/`dnf-automatic`, `firewalld`
   (not ufw), SELinux kept Enforcing (roles must work with it, never disable
   it). Accepted trade-off: Fedora's ~13-month lifecycle means a yearly OS
   upgrade, documented in the runbook.
2. **No security groups.** Closing public SSH after bootstrap is done by
   firewalld via Ansible (bootstrap.yml, phase 4), not by a tofu variable.
   The OVH edge firewall is a possible belt-and-suspenders (TODO(verify)).
3. **Local OpenTofu state.** No S3 backend for now: state is gitignored,
   lives on the operator machine, and joins the restic backup set in phase 8.
   `terraform/backend.tf` documents the migration target.
4. **Sizing.** 6 vCPU / 12 GB drives runner defaults: 2 ephemeral runners x
   MemoryMax=4G, CPUQuota=250% (variables, adjustable).
5. **No attached volume.** `/srv` and runner work dirs live on the VPS disk;
   resize is an OVH panel operation (runbook section 4).

## Consequences

- Positive: zero re-ordering; the Fedora+workstation parity makes local
  Molecule runs match production exactly.
- Negative: yearly OS upgrades; VPS firewall features are thinner than
  security groups; local state requires backup discipline (phase 8).
- Reversibility: `terraform/ovh/` is the reviewed migration path.

## Pros and cons of the options

### Adopt the VPS

- Good: immediate; honest; all deltas recorded here.
- Bad: interim by definition; some brief features (SG-driven bootstrap) need
  an Ansible-flavored replacement.

### Rebuild immediately

- Good: pure ADR 0002 from day one.
- Bad: throws away a working, paid-for host for no functional gain.

## Links

- ADR 0002 (target compute), ADR 0003 (tailscale-only access),
  ADR 0004 (vault secrets)
