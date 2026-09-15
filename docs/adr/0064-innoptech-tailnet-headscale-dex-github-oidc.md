---
status: proposed
date: 2026-09-15
---

# 0064. Innoptech tailnet: self-hosted headscale with Dex-brokered GitHub OIDC

> **Proposed - team review gate.** Part of the control-plane set 0064-0069;
> nothing below is implemented until the engineering team accepts it.

## Context and problem statement

[0003](0003-tailscale-only-access.md) chose Tailscale SaaS for
identity-bound, zero-inbound access, and
[0024](0024-interim-access-and-identity-constraints.md) deferred it because
no Innoptech tailnet existed. The picture changed: veggies becomes
multi-user (one agent VM per user, [0066](0066-isolation-unit-one-vm-per-user.md)),
each user must reach only their own node, a web control plane needs a
login, and the only identity Innoptech shares is GitHub org membership.
The operator's personal headscale on `coruscant` is out of bounds (other
tenants, personal domain).

## Decision drivers

- Access tied to GitHub identity, org members only, no second directory.
- Per-user network reach: a user sees their node and nothing else.
- One login surface for the tailnet and the web app.
- No per-seat SaaS cost; the control server is ours and reviewable code.
- Zero inbound ports on agent nodes stay true; the control-plane host is
  the one deliberate public surface.

## Considered options

- Tailscale SaaS (0003's original choice) with its own SSO.
- Self-hosted **headscale**, OIDC via **Dex** brokering GitHub (chosen).
- Headscale with pre-auth keys only (no SSO).
- GitHub OAuth directly in the web app + Dex only for headscale.

## Decision outcome

1. The Innoptech tailnet is a **headscale** server on the control-plane
   host `veggies-main` ([0065](0065-cloud-substrate-gcp-compute-engine.md)),
   behind Caddy TLS on `hs.<TODO(you) innoptech.com>`. Packaging: Fedora
   44 ships `headscale` and `caddy` rpms; the review decides rpm services
   vs a rootless quadlet under the `core` user (one container lifecycle
   model, the planner's preference). Either way the config is
   Ansible-templated: `server_url`, MagicDNS on, `base_domain` distinct
   from the server host, sqlite, public Tailscale DERP map (embedded DERP
   rejected: opens 3478/udp), `policy.mode file`.
2. **Dex** (pinned digest) is the single OIDC issuer. Its GitHub connector
   uses the `veggies-harness` App's OAuth client
   ([0063](0063-github-app-identity.md)) with `orgs: [Innoptech]`; static
   clients `headscale` and `veggies-core`. GitHub is OAuth2 without OIDC
   discovery, so a broker is required regardless of which app logs in.
3. **ACL policy is Ansible-owned** (templated from the node roster,
   validated at converge with `headscale policy check`): user ->
   `tag:node-<login>:22,4096-4196` only; admins -> every node `:22`;
   `tag:main-node` -> every node `:22,4096-4196` (the app's ssh and the
   kick traffic). Tags are owned by pre-auth keys, never by users.
4. **Session attach is tailnet-direct**: the opencode UI on the owner's
   node plus the per-stack password shown only to the owner in the app. A
   Caddy `forward_auth` reverse proxy is deferred (per-stack subdomains or
   SPA path rewriting, unverified).
5. The app's headscale API key is generated once at converge on the host
   and stored as a podman secret for `core` (crowdsec bouncer-key
   precedent), never in the vault; rotation is a tagged task.
6. `veggies-main` joins its own headscale via the tailscale role; the same
   host running both means a headscale outage cuts the main node's own
   tailnet path - recovery is GCP IAP SSH / serial console.

## Consequences

- Positive: org-member login for tailnet and app alike; non-members are
  refused at Dex; per-user reach is a policy file in git; no SaaS seats;
  MagicDNS names for every node.
- Negative / accepted: two more services to run (headscale, Dex) plus
  Caddy on a public 443; the operator's workstation must switch tailscale
  profiles between the personal and the Innoptech control server
  (TODO(verify) `tailscale switch` with distinct login servers); headscale
  user naming under OIDC (which claim becomes `<user>@` in policy) and the
  Dex GitHub connector's claim names are TODO(verify) before the ACL is
  written.
- On acceptance: 0003 -> `accepted (amended by 0064)`; 0024 gate #1's
  re-entry path is replaced by this ADR; deviation ledger row
  "Tailscale-only access" -> "self-hosted headscale tailnet; public 80/443
  on the control-plane host only".

## Pros and cons of the options

### Tailscale SaaS

- Good, because zero control-plane to run; 0003 already assumed it.
- Bad, because identity would be Tailscale's SSO (another directory or a
  paid tier for custom OIDC), seat cost per user, and the org control that
  made GitHub the identity source is lost.

### Headscale + Dex (chosen)

- Good, because GitHub org membership is the one identity, the ACL is
  reviewable code, and both the tailnet and the app share one login.
- Bad, because we run the control server; mitigated by boring packaging
  and IAP break-glass.

### Headscale with pre-auth keys only

- Good, because no OIDC at all.
- Bad, because no self-service enrollment, no login for the web app, and
  the operator hands out keys forever.

### App -> GitHub OAuth directly, Dex only for headscale

- Good, because one fewer OIDC client.
- Bad, because two login surfaces, two org checks, a second callback URL on
  the App, and the app loses Dex's `groups` claim.

## Links

- 0003, 0024, 0063, 0065, 0066, 0067; issue #56 (identity), discussion #42.
