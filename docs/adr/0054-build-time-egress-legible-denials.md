---
status: accepted
date: 2026-09-11
---

# 0054. Build-time egress: no separate lane; the CLI surfaces proxy denials by domain

## Context and problem statement

Remote image builds (`veggies prepare` / `veggies up` -> `ensure_images`)
run as the egress-denied `stacks` user and fetch through the substrate
squid allowlist (ADR 0006/0016). A build step reaching a non-allowlisted
domain died as a bare proxy 403 inside a build layer: no domain named,
no hint which list denied it, no remedy (issue #70, discussion #64).
Overlay stacks (#69) add more toolchains to the build path and inherit
this failure mode.

## Decision drivers

- The substrate allowlist stays tight: it is the exfiltration boundary
  (ADR 0006); build convenience must not widen it.
- A denial must be legible at failure time: which domains, during which
  window, and the exact remediation.
- One authoritative surface for what was denied, not per-tool heuristics.

## Considered options

- A: No build-time lane; the CLI reads the substrate squid access log on
  build failure and names the denied domains (chosen)
- B: A build-time egress lane (extra domains scoped to builds)
- C: Parse per-tool build output for the blocked domain
- D: Do nothing until overlays (#69) force the question

## Decision outcome

Option A. The allowlist is unchanged - there is no build-time egress
lane. On a remote build/pull failure the CLI reads
`podman logs --since <window> squid` as the `egress-proxy` user over
ssh+sudo, partitions the `TCP_DENIED/403` lines by client (loopback =
this build's path; anything else = pod/runtime traffic sharing the
host-wide log), and raises a legible error naming the image, every
loopback-denied domain in the window, a paste-ready
`egress_allowlist_extra` YAML block, `mask converge`, the
build-elsewhere-and-pull alternative (ghcr.io is already allowlisted),
and the runbook pointer. The original build error is chained, never
replaced. When the window shows no denials (or the log is unreadable),
the original error is re-raised with a hint covering proxy-bypassing
tools, which hit the per-UID nftables drop instead
(`journalctl -k -g infra-egress-deny`).

Why B fails even though squid can express it: squid ACLs can scope by
source IP, and build traffic is even distinguishable there (loopback vs
the pasta gateway). The lane still fails, because what a build needs is
a DOMAIN, and a domain cannot be scoped to builds: a dstdomain added
"for builds" lands in one allowlist permanently live for EVERY host-side
proxy user (builds, host processes, runner jobs), and host source-IP is
not a trustworthy "this is a build" marker. Domain-level build scoping
is unenforceable; the only real option was runtime widening, rejected
under ADR 0006.

Option C was rejected: every tool reports a proxied 403 differently
(curl exit 56, npm's tunneling text), and the parse breaks per tool. The
squid access log is the one authoritative surface - the deny decision is
made there and logged there.

Option D was rejected: a non-allowlisted registry on the PULL path fails
today, before any overlay arrives - ghcr.io is allowlisted, an overlay's
`FROM registry.example.com/...` is not, and the same opaque 403 results.

## Consequences

- Positive: a blocked build names its domains and the fix in one error;
  the allowlist stays exactly as tight as ADR 0006 drew it.
- Negative: the stack-layer CLI now consumes a SUBSTRATE log surface - a
  deliberate, pinned exception to the ADR 0016 boundary (ansible still
  owns the proxy; the CLI only reads its log). The contract is: user
  `egress-proxy` (the role's `egress_proxy_user` default; a group_vars
  override silently degrades the diagnostic to the no-denials hint -
  accepted), container `squid` (the quadlet's `ContainerName`), and
  squid's native access-log format on stdout. Pinned by a pytest drift
  guard and by a comment at the `access_log` line in `squid.conf.j2`.
- The log read rides the same cloud-init `ALL=(ALL) NOPASSWD:ALL` sudo
  grant the existing `sudo -n -u stacks` transport already assumes
  (ADR 0014's recorded assumption); no new privilege is granted.
- Accepted caveat - log-window attribution: the substrate log is
  host-wide, so parallel builds and runtime traffic can coincide in one
  window. The client partition plus the "during this build's window"
  wording is the honesty mechanism, and a named denial is a review
  prompt, not a rubber stamp: storage.googleapis.com stays off the base
  list on purpose (it fronts all of GCS; pinned by test) no matter how
  often a build asks for it.
- `egress_allowlist_extra` is host-wide and permanent once converged -
  it serves every stack, runner job, and host build. For exotic or
  one-shot toolchains, prefer building the image in CI and pulling it
  (ghcr.io is allowlisted) over widening the host list.

## Pros and cons of the options

### A: CLI surfaces the squid log denials

- Good: one authoritative source for what was denied; zero egress-policy
  change; covers builds and pulls; remediation is paste-ready.
- Bad: cross-layer read that must stay pinned (tests + template
  comment); window attribution is probabilistic under parallel load.

### B: Build-time egress lane

- Good: builds fetch what they need without a converge.
- Bad: unenforceable scoping - a dstdomain entry is live for every
  host-side proxy user, not just builds; source-IP is not a trustworthy
  build marker; runtime widening under a build label.

### C: Per-tool output parsing

- Good: no new host access.
- Bad: fragile per tool; misses tools that hide the 403 entirely; the
  log already records the truth.

### D: Wait for #69

- Good: zero work now.
- Bad: the pull path fails today on any non-allowlisted registry - the
  problem predates overlays.

## Links

- ADR 0006 (the egress allowlist this keeps tight), ADR 0016 (the
  substrate/stack boundary this crosses read-only)
- Issue #70, discussion #64
- docs/runbook.md - "Image build fails with a blocked domain"
