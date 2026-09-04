# opencode

Persistent opencode agent server as a rootless quadlet under the admin user
(ADR 0012), with the vendored `agent-config/` baseline synced into
`~/.config/opencode/` on every converge (agent config changes are PRs).

## What it changes

- Builds `localhost/opencode:<version>` (Ubuntu 24.04 + opencode tarball,
  version-pinned; upstream publishes no checksums - see defaults note).
- Syncs `agent-config/` (opencode.json incl. the pinned Superpowers plugin,
  agents/, skills/) into the global config dir.
- Installs `auth.json` with the litellm virtual key (from the vault).
- Quadlet `opencode.container`: `opencode serve` on 127.0.0.1:4096, mounts
  config/data/`~/src` with SELinux `:Z`, `SUPERPOWERS_DISABLE_TELEMETRY=1`.
- Helpers in /usr/local/bin: `agent-attach` (tmux session in the container)
  and `agent-status` (sessions, runner units, litellm, disk).

## Variables

See `defaults/main.yml`. Gates for tests: `opencode_build_image`,
`opencode_start`, `opencode_sync_config`.

## Be careful

- Model routing lives in `agent-config/litellm/config.yaml` + the quadlet
  users' keys; opencode itself only knows the virtual key.
- The first `opencode serve` fetches the pinned Superpowers plugin from
  github + npm (host egress is unrestricted; runner containers are NOT the
  ones doing this).
