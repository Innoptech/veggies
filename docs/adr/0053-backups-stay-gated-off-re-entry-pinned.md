---
status: accepted
date: 2026-09-11
---

# 0053. Backups stay gated off: re-entry pinned as an operator procedure

## Context and problem statement

[0024](0024-interim-access-and-identity-constraints.md) gate #2 keeps the
backup role off (`backup_enabled: false` in the operator's group_vars) with
the re-entry trigger: "create the OVH bucket + S3 creds in
`secrets/infra.yml`, set `backup_repo`, flip the flag." That trigger sat
unfired since 2026-09-08. Meanwhile [0051](0051-spend-log-record-contract.md)
and [0052](0052-spend-log-writer.md) made the gate load-bearing: every model
call lands in `<state_root>/<stack>/spend.jsonl` - the per-issue cost ledger
the whole metering stack exists to justify - and that file's durability
today is one disk.

Issue #80 asked an unattended agent session to un-gate. It cannot:

- Creating the OVH bucket + S3 credentials is an account-mutating action;
  AGENTS.md rule 2 reserves those to explicit in-conversation human
  approval, and the kick pod verifiably holds no OVH/OpenStack credentials.
- The live switches are operator-local: `backup_enabled` / `backup_repo`
  sit in the gitignored `ansible/inventory/group_vars/all.yml`; the restic
  keys sit in the ansible-vault. There is nothing in git to flip.

So the decision the issue's own branch (b) anticipated falls due: record
the keep-off as deliberate, name what it costs, and pin the re-entry so
the human can execute it in minutes. Two more facts sharpen the cost:

- `backup_enabled` gates the WHOLE backup role in
  `ansible/playbooks/site.yml`, so the role-owned cleanup timers (stale
  runner workdir reaping, the 500 MB cache cap, the 80% disk alert) are
  off today too - arguably the sharper operational risk on a CI box.
- [0021](0021-stack-data-backup-and-restore.md) is still `proposed` and is
  not the backup decision of record; this ADR is, until re-entry.

## Decision

1. **The gate stays OFF - deliberate, not lapsed.** The bucket key is the
   human's: that boundary (AGENTS.md rule 2) is the design working, not
   the plan slipping. This ADR changes nothing on disk; spend-history
   durability is exactly as fragile after it as before.
2. **Re-entry is the pinned operator procedure in runbook section 6**
   ("Enabling backups for the first time"): create the container + S3
   creds, vault-edit the three restic keys, set `backup_repo` +
   `backup_enabled: true`, converge, smoke, restore-drill, close-out docs
   PR. About 15 minutes of operator time.
3. **Tripwire.** Re-entry becomes due the day a `spend.jsonl.4` segment
   exists on any stack - the next rotation past it destroys paid history -
   or at the next section-1 rebuild, whichever comes first. The rebuild
   checklist carries the box.
4. **No terraform for the bucket yet.** `terraform/ovh/` is a
   never-applied, compute-only scaffold (ADRs 0002/0008); the openstack
   provider can model a container but not the S3/ec2 credentials, which
   are an account-level OVH action. A validated-but-never-planned resource
   is speculative code. The bucket joins terraform when the ADR 0002
   migration makes the module live.
5. **Interim mitigation, documented not automated**: a manual ssh/tar pull
   of the stack state dir to the operator workstation (runbook cost
   section, "Backup status") moves the ledger from one disk to two with
   zero new accounts or secrets. It stays manual: an unattended sync
   daemon is new machinery the tripwire makes unnecessary.
6. **Criterion-2 pin**: `tests/test_backup.py` asserts the spend log's
   host directory equals the CLI's `REMOTE_STATE_ROOT` and sits in the
   role's `backup_paths`, so the rotated segments are provably inside the
   restic set the day the gate flips.

## Consequences

- Positive: the keep-off is explicit and reviewable instead of silent
  drift; un-gate is a ~15-minute pinned procedure with a restore drill
  built in; the tripwire converts open-ended deferral into an alarm; the
  cleanup-timer blast radius of the gate is now on record; the
  cross-boundary test pin guarantees `spend.jsonl*` is in the backup set
  whenever the flag flips.
- Negative / accepted, named per the issue's demand:
  - **Single-disk durability**: host death loses all spend history since
    the last manual export (i.e. potentially everything).
  - **`veggies down --purge` deletes spend history** with the state root
    (the CLI warns first) - unchanged by this ADR.
  - Rotation loss is NOT a gate consequence and this ADR does not fix it:
    the ~60 MiB cap drops the oldest segments even with backups on; a
    daily restic timer only bounds that loss window to 24 h once enabled.
    Record headroom at ~0.5-1 KB per record is tens of thousands of
    calls - comfortable but finite, hence the tripwire.
  - Local-workstation stacks have no backup under ANY gate state;
    re-entry fixes the VPS only.
  - This is the second consecutive keep-off (0024, now 0053). The
    tripwire + the section-1 checklist box are the forcing function; if
    both fail, the gate is de-facto permanent and should be re-decided
    honestly.

## Links

- Re-enters [0024](0024-interim-access-and-identity-constraints.md) gate
  #2; record contract [0051](0051-spend-log-record-contract.md), writer
  [0052](0052-spend-log-writer.md), metering rationale
  [0022](0022-cost-metering-and-model-routing.md); stack-state scope
  [0021](0021-stack-data-backup-and-restore.md) (still proposed - not the
  decision of record); scaffold posture
  [0002](0002-public-cloud-over-vps.md) /
  [0008](0008-interim-platform-vps-fedora-local-state.md).
- Issues: #80 (this re-entry), #46 (writer; its success criterion filed
  #80).
