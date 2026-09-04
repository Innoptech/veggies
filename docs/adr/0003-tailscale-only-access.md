---
status: accepted
date: 2026-09-03
---

# 0003. Tailscale-only network access

## Context and problem statement

The host runs autonomous agents that process untrusted input (issues, PR
bodies). Any public listening service is attack surface. The human still needs
SSH access, and GitHub must reach in (via the runner's outbound connection).

## Decision drivers

- Zero inbound public ports in steady state.
- No self-hosted VPN to maintain.
- Access tied to identity (tailnet ACLs), not to source IPs.

## Considered options

- Tailscale, tagged node, SSH only over the tailnet
- WireGuard by hand
- Public sshd behind source-IP allowlists

## Decision outcome

Tailscale. Bootstrap opens public SSH once (`base_public_ssh: true` in
`bootstrap.yml`); an unskippable verification play proves tailnet SSH works;
the closing play then removes public SSH via firewalld. Tailscale SSH is
enabled and sshd remains as fallback on the trusted `tailscale0` zone. The
ACL tag is `tag:agent-host` (placeholder in group_vars until the tailnet name
is supplied).

On the interim VPS (ADR 0008) there is no security group, so the close-out is
a firewalld change instead of a `tofu apply` variable flip. The Public Cloud
scaffold keeps the security-group variant for the future (`admin_cidr`).

## Consequences

- Positive: no public attack surface; per-identity ACLs; MagicDNS naming.
- Negative: tailnet dependency for admin access; if tailscale breaks on the
  host, recovery is the OVH console (runbook section 2).
- The ACL policy itself lives in the tailnet admin console / its own gitops
  repo - out of scope here (noted in the README).

## Pros and cons of the options

### Tailscale

- Good: zero config NAT traversal, identity-based, Tailscale SSH.
- Bad: external control plane dependency.

### Hand-rolled WireGuard

- Good: no third party.
- Bad: key management and roaming are ours to maintain - not boring.

### Source-IP sshd allowlist

- Good: no dependencies.
- Bad: source IPs change; still a public port; not identity-bound.

## Links

- ADR 0008 (interim platform - firewalld close-out)
- ansible/roles/tailscale, ansible/playbooks/bootstrap.yml
