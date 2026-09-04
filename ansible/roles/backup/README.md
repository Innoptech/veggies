# backup

restic backups to OVH Object Storage + daily hygiene timers (spec section 5).

## What it changes

- Installs `restic`; `/etc/restic/restic.env` (0600, from the vault:
  password, S3 creds, repository URL from `backup_repo` in group_vars).
- `backup.timer` (daily 03:00): `restic backup` of `backup_paths`
  (opencode config+sessions, both service users' configs, `/srv`, nftables,
  sshd drop-ins) + `forget --prune` with `backup_retention_*`.
- `restic-check.timer` (weekly): repository integrity check.
- `cleanup.timer` (daily 04:00): runs `cleanup.py` - prunes each service
  user's containers/images older than 7 days, deletes runner `_work` dirs
  older than 2 days, clears pip/npm caches over 500 MB, and alerts (log line
  + optional webhook) when disk use >= 80%.
- `/usr/local/sbin/restore.sh` - fresh-instance restore (runbook section 6).

## Variables

See `defaults/main.yml` and `group_vars/all.yml(.example)`:
`backup_retention_daily/weekly`, `backup_repo`, `backup_cleanup_*`,
`backup_cleanup_webhook_url` (vault; goes into the unit as `CLEANUP_WEBHOOK_URL`).

## Be careful

- The operator machine's tofu state is NOT on veggies - back it up from your
  workstation with the same vault creds (runbook section 6 note).
- First backup initializes the repo (`restic-prep` in ExecStartPre).
- `restore.sh` overwrites into `/` - read the runbook before running it.
