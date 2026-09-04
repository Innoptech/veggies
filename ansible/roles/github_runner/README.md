# github_runner

Ephemeral, rootless, quadlet-based GitHub Actions runners (ADRs 0005, 0009).

## Flow

1. `gh-runner@.container` (quadlet template) + instance symlinks
   (`gh-runner@<repo>-<n>.container` or `gh-runner@<n>.container` for
   `github_runner_scope: org`) live in `~gh-runner/.config/containers/systemd/`.
2. On every (re)start, systemd's `ExecStartPre` runs `fetch_runner_token`,
   which POSTs the GitHub API (PAT from `secrets/github.yml`, mode 0600 env
   file) for a **short-lived registration token** and writes the instance env
   file. No long-lived runner token exists anywhere.
3. The container registers with `--ephemeral --unattended --disableupdate`,
   runs exactly one job, exits; systemd restarts the unit with a fresh token.

## What it changes

- `/srv/gh-runner/<instance>` work dirs (the ONLY mounted path besides the
  rootless podman socket).
- Runner image `localhost/gh-runner:<version>` built from the in-repo
  Containerfile (Ubuntu 24.04, tarball pinned by version + sha256).
- Per-unit cgroup limits: `MemoryMax=4G`, `CPUQuota=250%` (defaults for
  6 vCPU / 12 GB; override via variables).
- `~gh-runner/.config/gh-runner/api.env` (0600, from the vault).

## Variables

See `defaults/main.yml`. Notables: `github_runner_scope` (repo|org),
`github_runner_count`, `github_runner_memory_max`, `github_runner_cpu_quota`,
`github_runner_labels`, `github_runner_version`/`_sha256` (both pinned; bump
together). Test gates: `github_runner_build_image`, `github_runner_register`,
`github_runner_start`.

## Be careful

- The docker socket inside runner containers is the gh-runner user's ROOTLESS
  podman socket: workflow `container:` jobs and `docker` steps work, but
  privileged containers and true DinD do not. Documented limitation.
- `scope=repo` multiplies runners by repo count. On 12 GB RAM keep
  `count x repos x 4G` within budget.
- Never commit a registration token; the fetcher exists precisely to avoid
  storing one.
