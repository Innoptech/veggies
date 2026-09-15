---
status: proposed
date: 2026-09-15
---

# 0069. Backups live: restic to GCS per node; stack volumes exported; the 0054 gate closes

> **Proposed - team review gate.** Part of the control-plane set 0064-0069.

## Context and problem statement

[0021](0021-stack-data-backup-and-restore.md) left stack-volume backup as
open questions; [0024](0024-interim-access-and-identity-constraints.md)
gate #2 and [0054](0054-backups-stay-gated-off-re-entry-pinned.md) keep
the restic role off until an operator creates the OVH bucket and
credentials, and 0054 asked for a new ADR at un-gate time. The substrate
is now GCP ([0065](0065-cloud-substrate-gcp-compute-engine.md)), the
operator asked for opencode session state to reach object storage now,
and every user node ([0066](0066-isolation-unit-one-vm-per-user.md))
carries session history and the 0051 spend ledger on one disk.

## Decision drivers

- Session history (`veggies-<name>-opencode` volumes) and `spend.jsonl*`
  survive a node loss.
- No long-lived object-storage keys on nodes.
- Restore is drilled, not assumed.
- Same role, same timers; only the backend and the capture list change.

## Considered options

- restic -> GCS via HMAC S3-interoperability keys.
- **restic -> GCS (`gs:` backend) with the node's attached service
  account, per-node prefix** (chosen).
- GCE persistent-disk snapshots only.

## Decision outcome

1. `backup_enabled: true` on every GCP node. Target
   `gs:<bucket>:/<inventory_hostname>` with Application Default
   Credentials from the metadata server (TODO(verify) restic `gs:` with
   ADC; fallback: a per-node SA key file at `/etc/restic`, still no HMAC).
   Per-node service account, `roles/storage.objectUser` conditioned to
   the node's prefix (TODO(verify); fallback: one bucket per node).
   Bucket: versioning on, 30-day noncurrent-version lifecycle,
   public-access prevention; the restic password stays in the vault.
2. **Captured** on agent nodes: the stacks state root (registry, per-stack
   config, `spend.jsonl*`, `pr-review-verdicts.jsonl`) **minus `clones/`**,
   plus a `podman volume export` of every `veggies-<name>-opencode`
   volume written into `<state_root>/volumes/` by a tested
   `ExecStartPre` script that reads the node-side `state.json`. On the
   main node: exports of the headscale, Dex and app data volumes (the app
   runs a nightly `sqlite3 .backup`).
3. Retention 7 daily / 4 weekly; weekly `restic check`; nightly timer.
4. **Restore** = `restore.sh` + `podman volume import` + `veggies up`; a
   restore drill per node is part of the first-node acceptance.
5. Runbook §6's first-enable procedure is rewritten for GCS and executed
   as part of the first GCP user node.

## Consequences

- Positive: a node is disposable; session memory and the cost ledger
  outlive it; no static storage keys anywhere.
- Negative / accepted: `clones/` is excluded, so unpushed worktree commits
  of a crashed session die with the disk (the review decides whether to
  include `clones/*/.veggies/wt`); volume exports double the storage of
  session data during the backup window.
- On acceptance: 0021 -> `superseded by ADR-0069`; 0054 ->
  `(amended by 0069)`; 0024 gate #2 closed; ledger row "Backups from day
  one" -> "live: restic -> GCS per node".

## Pros and cons of the options

### HMAC S3-interop keys

- Good, because the current role's S3 template works unchanged.
- Bad, because long-lived static keys on every node.

### `gs:` with the attached service account (chosen)

- Good, because no key material on disk; IAM revocation is instant.
- Bad, because two TODO(verify)s (ADC support, prefix conditions) with
  named fallbacks.

### Disk snapshots only

- Good, because zero in-guest tooling.
- Bad, because no file-level restore, no cross-project portability, and
  crash-consistent at best for sqlite and volume data.

## Links

- 0021, 0024, 0051, 0054, 0065, 0066; `ansible/roles/backup/`, runbook §6.
