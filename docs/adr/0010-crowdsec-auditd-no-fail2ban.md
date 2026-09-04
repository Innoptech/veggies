---
status: accepted
date: 2026-09-03
---

# 0010. CrowdSec + auditd instead of fail2ban

## Context and problem statement

The brief's base role listed fail2ban. The operator asked for CrowdSec "and
other tools you deem right". Two ban systems parsing the same logs is an
anti-pattern (conflicting bans, double the state).

## Decision drivers

- One log-reaction system, not two.
- A forensic trail independent of any agent's actions.
- Honest about value: after ADR 0003, the public surface is near zero.

## Considered options

- CrowdSec engine + nftables bouncer, no fail2ban
- fail2ban as briefed
- both

## Decision outcome

CrowdSec + `crowdsec-firewall-bouncer-nftables`, with the `crowdsecurity/linux`
and `crowdsecurity/sshd` collections; no console enrollment by default (no
external account). Plus `auditd` with a minimal watch set (ssh/sshd config,
sudoers, account databases, container and firewalld config) for forensics.
fail2ban is dropped. SELinux stays Enforcing and roles must cope (they do;
the base image and tasks are built for it).

## Consequences

- Positive: community blocklists cover the bootstrap window and any future
  exposed service; auditd gives a tamper-evident trail for "what did that
  agent change".
- Negative: crowdsec adds a third-party dnf repo and hub traffic from the
  host (host egress is intentionally unrestricted - ADR 0006 constrains
  agent users, not the host).
- Rejected extras: AIDE/rkhunter (noise on a CI-mutating host), OSSEC (heavy).

## Pros and cons of the options

### CrowdSec + auditd

- Good: one reaction system; forensic trail; Fedora-native bouncer (nftables).
- Bad: extra repo; modest value while nothing public listens.

### fail2ban

- Good: minimal, ubiquitous.
- Bad: duplicates CrowdSec's job with fewer signals.

### both

- Good: none.
- Bad: two ban writers on the same firewall and logs.

## Links

- ansible/roles/crowdsec, ansible/roles/base (auditd)
- ADR 0003 (why the surface is small), ADR 0006 (egress side)
