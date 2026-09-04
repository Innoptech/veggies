# crowdsec

CrowdSec security engine + nftables firewall bouncer (ADR 0010). Replaces
fail2ban: one log-parsing/brute-force system, with community blocklists.

## What it changes

- Adds the crowdsec packagecloud repo (gpgcheck + repo_gpgcheck on).
- Installs `crowdsec` and `crowdsec-firewall-bouncer-nftables`.
- Enables both services; updates the hub and installs the
  `crowdsecurity/linux` + `crowdsecurity/sshd` collections.
- Optional console enrollment when `crowdsec_enroll_key` is set (vault).

## Variables

See `defaults/main.yml`. `crowdsec_setup_hub` and `crowdsec_start_services`
are container/Molecule gates - do not change them on real hosts.

## Be careful

- Honest scope note: once public SSH is closed (ADR 0003), the public attack
  surface is ~zero. CrowdSec's value here is the bootstrap window, future
  exposed services, and visibility (metrics/decisions in `cscli`).
- The bouncer enforces via nftables; it coexists with firewalld (firewalld
  owns its tables, the bouncer owns its own set/table). TODO(verify): exact
  table names if a conflict ever appears.
- Hub updates and collection installs reach `hub.crowdsec.net` from the HOST
  (unrestricted by design - the egress allowlist constrains agent users only,
  ADR 0006).
