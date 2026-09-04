# egress

Egress allowlist for agent workloads (ADR 0006). Two cooperating mechanisms:

1. **squid** filtering forward proxy (rootless quadlet under `egress-proxy`,
   published on `0.0.0.0:3128`): domain allowlist (squid `dstdomain`), source
   ACLs, denials logged to stdout -> journald.
2. **nftables per-UID policy** (`inet infra-egress`, own table - firewalld
   never touches it): the `gh-runner` user may reach only the proxy and the
   litellm gateway (`{127.0.0.1, 169.254.1.2}:{3128,4000}`); everything else
   is `log` + `drop`. Because rootless podman (pasta) makes all container
   traffic appear as the owning UID's processes, this covers ANY process
   running as that user, container or not.

The host itself, `fedora`, and `egress-proxy` are intentionally unrestricted.

## Why PublishPort is 0.0.0.0

Verified 2026-09-03: rootless containers (pasta) cannot reach a
host-loopback-only service; they reach the host via `host.containers.internal`
(169.254.1.2). firewalld's public zone still denies external access, and the
squid source ACL (loopback/link-local/tailnet/RFC1918) plus the domain
allowlist bound what the tailnet can do with it.

## Variables

See `defaults/main.yml`. Domain list = `egress_allowlist_base` +
`egress_model_endpoints` + `egress_allowlist_extra` (group_vars).

## Be careful

- The deny rule is per-UID: if you ever run something as `gh-runner` by hand,
  it is also restricted (that's the point).
- Adding a provider = add its endpoint to `egress_model_endpoints`, PR,
  converge. Denials to review: `journalctl -k -g infra-egress-deny`.
- DNS from runner containers is denied by design (the proxy resolves). Tools
  that insist on direct DNS/TCP will fail - logged, reviewable, intended.
