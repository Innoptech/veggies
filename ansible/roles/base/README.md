# base

Foundation hardening for `veggies` (Fedora 44, SELinux kept Enforcing).

## What it changes

- Packages: firewalld, dnf-automatic, chrony, audit, openssh-server, tmux,
  git, SELinux python tooling.
- `fedora` (admin): wheel group, your ssh keys (`admin_ssh_public_keys`).
- Service users `gh-runner` / `egress-proxy` (nologin, linger enabled,
  subuid/subgid allocated) for the rootless quadlets (ADR 0009).
- sshd drop-in `50-infra.conf`: key-only, no root, no X11/tunnel, local tcp
  forwarding only (validated with `sshd -t` before activation).
- firewalld: default-deny posture; public `ssh` only when
  `base_public_ssh=true` (bootstrap window - the steady state is closed,
  ADR 0003).
- dnf-automatic (security updates applied), chronyd, journald size limits,
  sysctl drop-in, swapfile, auditd watches (ADR 0010).

## Variables

See `defaults/main.yml`. The gates `base_manage_swap`, `base_apply_sysctls`,
`base_manage_auditd` exist for containers/Molecule; do not set them on real
hosts.

## Be careful

- A broken sshd change can lock you out. The template validates before
  install; still, bootstrap is the only time public SSH exists - after that,
  recovery is the OVH console (runbook section 2).
- `base_public_ssh: true` is for `bootstrap.yml` only. `site.yml` relies on
  the default (`false`).
