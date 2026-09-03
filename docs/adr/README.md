# Architecture decision records

New decisions: copy `0000-madr-template.md`, next free number, one topic per
file. Never edit a decided ADR - write a new one that supersedes it.

| ADR | Title | Status | Phase |
|-----|-------|--------|-------|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted | 1 |
| 0002 | OVH Public Cloud over VPS (target architecture) | proposed | 3 |
| 0003 | Tailscale-only network access | proposed | 4 |
| 0004 | Secrets in git via ansible-vault (supersedes the brief's sops+age) | accepted | 1 |
| 0005 | Ephemeral containerised GitHub runners | proposed | 5 |
| 0006 | Egress allowlist: squid proxy + per-UID nftables | proposed | 7 |
| 0007 | GitHub policy as code | proposed | 2 |
| 0008 | Interim platform: manually-rented VPS, Fedora 44, local state | accepted | 1 |
| 0009 | Rootless Podman + Quadlet instead of Docker | accepted | 1 |
| 0010 | CrowdSec + auditd instead of fail2ban | accepted | 4 |
| 0011 | LiteLLM gateway as the model router; keys held only by the proxy | accepted | 6 |
| 0012 | Vendored agent-config baseline; Superpowers as a pinned opencode plugin | accepted | 6 |
