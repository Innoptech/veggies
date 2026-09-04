#!/usr/bin/env bash
# Ephemeral runner entrypoint: register with the short-lived token from the
# EnvironmentFile (written by fetch_runner_token via ExecStartPre), run exactly
# one job, exit. systemd restarts the unit, which fetches a fresh token.
set -euo pipefail

: "${RUNNER_TOKEN:?missing - fetch_runner_token did not run or failed}"
: "${RUNNER_URL:?missing}"

cd /home/runner

./config.sh \
  --url "$RUNNER_URL" \
  --token "$RUNNER_TOKEN" \
  --name "${RUNNER_NAME:-$(hostname)}" \
  --labels "${RUNNER_LABELS:-self-hosted,linux,x64}" \
  --work _work \
  --ephemeral \
  --unattended \
  --disableupdate

exec ./run.sh
