# Architecture decision records

New decisions: copy `0000-madr-template.md`, next free number, one topic per
file. Never edit a decided ADR - write a new one that supersedes it.

| ADR | Title | Status | Phase |
|-----|-------|--------|-------|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted | 1 |
| [0002](0002-public-cloud-over-vps.md) | OVH Public Cloud over VPS (target architecture) | accepted (deferred) | 3 |
| [0003](0003-tailscale-only-access.md) | Tailscale-only network access | accepted | 4 |
| 0004 | Secrets in git via ansible-vault (supersedes the brief's sops+age) | accepted | 1 |
| [0005](0005-ephemeral-containerised-runners.md) | Ephemeral containerised GitHub runners | accepted | 5 |
| [0006](0006-egress-allowlist.md) | Egress allowlist: squid proxy + per-UID nftables | accepted | 7 |
| [0007](0007-github-policy-as-code.md) | GitHub policy as code | accepted | 2 |
| [0008](0008-interim-platform-vps-fedora-local-state.md) | Interim platform: manually-rented VPS, Fedora 44, local state | accepted | 1 |
| 0009 | Rootless Podman + Quadlet instead of Docker | accepted | 1 |
| [0010](0010-crowdsec-auditd-no-fail2ban.md) | CrowdSec + auditd instead of fail2ban | accepted | 4 |
| [0011](0011-litellm-gateway-model-routing.md) | LiteLLM gateway as the model router; keys held only by the proxy | accepted | 6 |
| [0012](0012-agent-config-baseline-superpowers.md) | Vendored agent-config baseline; Superpowers as a pinned opencode plugin | accepted | 6 |
| [0013](0013-repo-scoped-agent-stacks.md) | Repo-scoped agent stacks via the `garden` CLI | accepted | 10 |
| [0014](0014-remote-stacks-over-ssh.md) | Remote stacks over ssh; CLI owns stacks, Ansible owns the host | accepted | 13 |
