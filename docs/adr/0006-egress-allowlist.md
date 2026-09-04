---
status: accepted
date: 2026-09-03
---

# 0006. Egress allowlist for agent workloads

## Context and problem statement

Agents read untrusted input (issue/PR text) and run with tool access. A
prompt injection that reaches arbitrary hosts can exfiltrate code or pivot.
The host's own updates must stay unrestricted.

## Decision drivers

- Constrain the RUNNER user (untrusted workloads), not the host.
- Work with rootless podman (no daemon-managed networks to anchor rules on).
- Every denial logged; mechanism boring enough to audit in minutes.

## Considered options

- Filtering forward proxy (squid) + per-UID nftables deny (chosen)
- nftables-only on a dedicated container network
- eBPF/LSM-level egress control (Cilium-style)

## Decision outcome

Two layers. squid (rootless quadlet, `egress-proxy` user) enforces the DOMAIN
allowlist: GitHub, package registries, distro mirrors, container registries,
and `egress_model_endpoints` (Fireworks). nftables (`inet infra-egress`)
enforces the TRANSPORT boundary for the `gh-runner` UID: only
`{127.0.0.1, 169.254.1.2}:{3128,4000}`; everything else logged and dropped.
This works because pasta makes container traffic appear as the owning UID.

Measured facts this design rests on (2026-09-03, this workstation): a
rootless container cannot reach a service published on host loopback; it
reaches 0.0.0.0-published services via `host.containers.internal`
(169.254.1.2). Hence squid and litellm publish on 0.0.0.0, protected by
firewalld default-deny + squid source ACLs + (litellm) key auth.

## Consequences

- Positive: a prompt-injected job can reach an allowlist of domains only,
  through a logged proxy, with no usable provider keys (ADR 0011).
- Negative: tools that bypass proxy env vars or need direct DNS fail
  (intended; logged; fixable by extending the allowlist).
- The runner token fetcher and image builds route through the proxy too.

## Pros and cons of the options

### squid + per-UID nftables

- Good: domain-level control (github.com IPs churn - raw nft can't do DNS);
  UID matching survives any container topology; both layers log.
- Bad: proxy env hygiene required; two components.

### nftables-only dedicated network

- Good: one mechanism.
- Bad: IP-based allowlists for GitHub are fragile (CDN churn); rootless
  per-user networks make a shared "agent net" impossible across users.

### eBPF/LSM

- Good: strongest.
- Bad: the opposite of boring for a single host.

## Links

- ansible/roles/egress, docs/threat-model.md, ADR 0011
