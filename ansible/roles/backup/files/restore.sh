#!/usr/bin/env bash
# restore.sh - restore veggies's state onto a fresh instance.
#
# Usage (as root, AFTER the base role created the users):
#   restore.sh /path/to/restic.env
#
# The env file carries RESTIC_PASSWORD, AWS_* and RESTIC_REPOSITORY (on a
# converged host it is /etc/restic/restic.env). Exercised by the phase 9
# rebuild checklist.
set -euo pipefail

ENV_FILE="${1:-/etc/restic/restic.env}"
[ -r "$ENV_FILE" ] || { echo "env file $ENV_FILE not readable"; exit 2; }

command -v restic >/dev/null || { echo "installing restic"; dnf install -y restic; }

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

echo "== snapshots available:"
restic snapshots --compact
printf "Restore LATEST snapshot to / ? [type: restore] "
read -r answer
[ "$answer" = "restore" ] || { echo "aborted"; exit 1; }

restic restore latest --target /
echo "done. Review permissions under /home/*, then converge."
