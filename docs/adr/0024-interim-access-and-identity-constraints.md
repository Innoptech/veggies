---
status: accepted
date: 2026-09-08
---

# 0024. Interim access and identity constraints (public SSH, backups off, relaxed self-review)

## Context

The first live bootstrap of veggies (2026-09-08) runs under real access
constraints that the target architecture (ADRs 0003, 0007) did not
anticipate:

- The workstation's tailscale client points at an unrelated Headscale
  server (`hyperpath.glo.quebec`). There is no Innoptech tailnet and we
  currently have no access to create or use one.
- No OVH Object Storage bucket exists yet for restic.
- The operator is a solo developer in the Innoptech GitHub org: cannot
  create bot accounts or org-level resources. Authentication is a
  fine-grained PAT on the operator's own account, scoped to
  `Innoptech/veggies`. GitHub Apps cannot be CODEOWNERS members, and the
  runner role + tofu provider authenticate with the PAT.
- GitHub never lets a PR author approve their own PR, so the ADR 0007
  policy (1 approval + code-owner review) applied to a repo whose only
  code owner is the sole author makes that author's own PRs unmergeable -
  a permanent wedge, with `enforce_admins` removing the escape hatch.

## Decision

Three interim relaxations, each gated by an explicit switch and a re-entry
trigger:

1. **Access**: the tailscale role and bootstrap's tailnet gate / public-SSH
   close-out are gated behind `tailscale_enabled` (default `true`; `false`
   in group_vars for now). Public SSH stays open, hardened: key-only sshd
   drop-in, firewalld default-deny, CrowdSec + nftables bouncer.
   `base_public_ssh: true` in group_vars - without it, converge would close
   public SSH and lock the operator out. ADR 0003 remains the target.
   *Re-entry*: an Innoptech tailnet exists → auth key in
   `secrets/infra.yml`, `tailscale_enabled: true`, re-run bootstrap (gate +
   close-out execute), `base_public_ssh: false`, `Host veggies` → MagicDNS
   name.
2. **Backups**: the backup role is gated behind `backup_enabled` (default
   `true`; `false` for now). No backups run. *Re-entry*: create the OVH
   bucket + S3 creds in `secrets/infra.yml`, set `backup_repo`, flip the
   flag.
3. **Review policy on the infra repo**: the tofu github module gains
   per-repo review overrides (`review_overrides`); `veggies` gets 0
   required approvals and no code-owner requirement. All other protections
   stand (required checks, linear history, no force-push, conversation
   resolution, the `production-infra` environment gate). This weakens ADR
   0007 on this repo only. *Re-entry*: a second GitHub identity exists (bot
   account with org access) → CODEOWNERS lists human + bot, override
   removed.

## Consequences

- While public SSH is open, brute-force surface returns; mitigations are
  key-only auth and CrowdSec. Remote stack attach uses `ssh -L` forwarding
  (the sshd drop-in allows local forwarding only).
- `infra-apply.yml` stays disabled: its converge job assumes Tailscale SaaS
  OAuth, and its Actions secrets/variables are not wired.
- No restore capability until backups are enabled; the backup/restore boxes
  in the rebuild checklist (runbook §1) stay unticked.
- Merges to the infra repo require green checks but no review; the human
  remains the gate by being the only person who merges.
