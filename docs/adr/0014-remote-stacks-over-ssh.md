---
status: accepted
date: 2026-09-04
---

# 0014. Remote garden stacks over ssh; CLI owns stacks, Ansible owns the host

## Context and problem statement

Stacks (ADR 0013) must run identically on the VPS (`garden --host garden up`).
The VPS is otherwise Ansible-managed. A clear ownership boundary and a boring
transport are required.

## Decision drivers

- One CLI codebase; remote is a transport difference, not a fork.
- No new host attack surface: reuse the tailnet-only sshd (ADR 0003).
- Agent workloads stay egress-restricted on the VPS (ADR 0006).
- Ansible remains the only manager of host-level state.

## Decision outcome

**Ownership boundary**: Ansible owns users, packages, firewall, egress policy,
and the `stacks` service user (in `base_service_users` and `podman_users`;
linger via base). The `garden` CLI owns everything below
`/home/stacks/.local/state/garden/` plus stacks' podman objects and user
units - the same split as the self-managing runner containers.

**Transport**: `ssh <alias> sudo -n -iu stacks <cmd>` for podman/systemctl/
file writes (stdin pipes carry kube YAML and secrets; nothing secret is ever
written on either side). Assumes passwordless sudo for the admin user, true
for OVH cloud images - TODO(verify) at first bootstrap; if false, base adds a
sudoers drop-in.

**Remote specifics**:
- Repos are clone-mode only remotely (a local bind-mount is meaningless).
  Private repos clone over https with the vault's github_token in a git
  `http.extraHeader` - briefly visible in the VPS process list; recorded in
  the threat model. TODO(verify): a cleaner credential flow later.
- opencode publishes `0.0.0.0:<port>` (firewalld default-deny guards public;
  the tailnet is the intended path); attach is `opencode attach
  http://<tailnet-name>:<port>` with the per-stack basic-auth password.
- The `stacks` uid joins `egress_denied_users`: stack containers can reach
  only the in-pod proxy/gateway; the nftables ruleset loops the list.
- The egress nftables template is now a per-user loop (`egress_denied_users`
  replaces the single `egress_runner_user`).

## Consequences

- Positive: `garden --host garden up` needs no new listening service, no API
  daemon, no ansible run per stack; local and remote share renderers and tests.
- Negative: ssh+sudo wrapping is chatty (a few seconds per operation);
  remote ops need the operator's ssh agent and the tailnet up.
- The litellm config reaches the VPS as a rendered copy in the stack config
  dir (locally it is a live mount of agent-config/); editing it means a
  `garden up` re-run, which is the intended flow anyway.

## Links

- cli/garden.py (host_* helpers), ansible/roles/{base,podman,egress},
  ADR 0013, ADR 0006, ADR 0003
