# tailscale

Installs tailscale, joins `garden` to the tailnet with a pre-authorized key
from the vault, advertises the ACL tag, enables Tailscale SSH, and binds
`tailscale0` to the trusted firewalld zone (sshd stays as the fallback path).
ADR 0003.

## What it changes

- Adds the tailscale stable repo (gpgcheck + repo_gpgcheck on).
- Installs `tailscale`, enables `tailscaled`.
- `tailscale up` with `--advertise-tags`, `--hostname`, `--ssh` when
  `tailscale_auth_key` is set (from `secrets/infra.yml`; the task is `no_log`).
- firewalld: `tailscale0` -> trusted zone (only after the node is Running).

## Variables

See `defaults/main.yml` and `group_vars/all.yml(.example)`:
`tailscale_tailnet`, `tailscale_tag`, `tailscale_hostname`, `tailscale_ssh`.

## Be careful

- With an empty `tailscale_auth_key` the role installs but does NOT join -
  that is the Molecule path, and also the safe failure mode.
- Never log the auth key. Do not remove `no_log: true` from the join task.
- The trusted-zone binding trusts the whole tailnet; ACLs at the tailnet
  level are the real boundary (tag `tag:agent-host`, restrict who can reach
  port 22).
