---
status: accepted
date: 2026-09-03
---

# 0009. Rootless Podman + Quadlet instead of Docker

## Context and problem statement

Agent workloads on this host run in containers, and runner jobs execute
untrusted PR content (ADR 0005). The original project brief specified a
Docker daemon. The host is one box running Fedora 44 (ADR 0008): which
container engine, and which unit model? The decision was taken on
2026-09-03 in the founding conversation and recorded retroactively on
2026-09-12 per issue #92, reconstructed from the deviation ledger, the
ADRs that cite it (0005, 0013), and the `ansible/roles/podman` role that
embodies it.

## Decision drivers

- Blast radius: even a container escape lands in an unprivileged user -
  ADR 0005 relies on exactly this.
- No root daemon: no privileged dockerd and no daemon-owned networks -
  a smaller attack surface.
- podman is the Fedora-native engine on the ADR 0008 platform.
- Quadlet units are systemd-native; for one box that beats a container
  orchestrator (boring over clever, AGENTS.md rule 7).
- Per-user pods match the per-UID egress model (ADR 0006) and the
  `gh-runner` user layout.

## Considered options

- Docker Engine + dockerd (the brief's pick)
- Rootless Podman + Quadlet

## Decision outcome

Rootless Podman + Quadlet, per user. The `ansible/roles/podman` role is
the contract:

- No docker-ce, no docker daemon, no `podman-docker` shim on the host.
- cgroup controllers (cpu/cpuset/io/memory/pids) are delegated to user
  managers, so `MemoryMax=`/`CPUQuota=` in user quadlets really limit.
- Each container user gets a quadlet dir
  `~/.config/containers/systemd/`, a `containers.conf` (journald logging,
  systemd cgroups), and a symlink-enabled per-user `podman.socket` - the
  docker-compatible API the runner containers use, so workflow `docker`
  steps become sibling rootless containers.
- SELinux stays Enforcing; volume mounts in quadlets need `:Z`/`:z`.

## Consequences

- Positive: an escape lands in an unprivileged user with no sudo and no
  daemon to talk to; systemd owns lifecycle and logs; nothing root-owned
  runs the containers.
- Negative / accepted: no privileged jobs and no true DinD under rootless
  podman - ADR 0005 records this limitation. Rootless networking runs via
  pasta/slirp4netns instead of daemon-managed bridges.
- Later: ADR 0025/0028 verified the cost of giving a stack component the
  podman socket; the socket is now banned from stack pods. The runner
  user's own socket, mounted into runner containers only, is unaffected.

## Pros and cons of the options

### Docker Engine + dockerd

- Good: the default every workflow with `docker` steps assumes.
- Bad: a root daemon turns a container escape into root on the host, and
  docker-ce is a foreign package set on Fedora.

### Rootless Podman + Quadlet

- Good: unprivileged escapes, plain systemd units, the OS's own engine.
- Bad: no privileged jobs or true DinD, and rootless networking via
  pasta/slirp4netns has its own quirks.

## Links

- Cited by: ADR 0005 (ephemeral runners), ADR 0013 (repo-scoped agent
  stacks)
- Platform: ADR 0008 (Fedora 44)
- Later: ADR 0025/0028 (component podman-socket access; socket now
  banned)
- Issue: <https://github.com/Innoptech/veggies/issues/92>
