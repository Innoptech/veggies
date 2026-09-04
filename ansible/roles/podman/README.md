# podman

Rootless Podman wiring for the quadlet users (ADR 0009). No docker-ce, no
docker daemon, no `podman-docker` shim on the host.

## What it changes

- Installs `podman`, `crun`, `pasta`, `slirp4netns`, `fuse-overlayfs`.
- Delegates cgroup controllers (cpu/cpuset/io/memory/pids) to user managers so
  `[Service] MemoryMax=`/`CPUQuota=` in user quadlets really limit.
- Per user in `podman_users`: `~/.config/containers/systemd/` (quadlet dir),
  `containers.conf` (journald logging, systemd cgroups), and a symlink-enabling
  of the per-user `podman.socket` (docker-compatible API, used by runner
  containers).

## Variables

See `defaults/main.yml`. Users and linger come from the `base` role.

## Be careful

- The socket symlink approach works without a running user manager; do not
  replace it with ad-hoc `systemctl --user enable` (fails for logged-out users).
- SELinux stays Enforcing: volume mounts in quadlets need `:Z`/`:z`.
