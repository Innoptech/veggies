#!/bin/sh
# veggies github-auth (ADR 0063): gh only reads a static GH_TOKEN, so hand it
# the sidecar's rotating installation token per invocation. Installed by the
# opencode wrapper at /root/.local/bin/gh, ahead of /usr/bin/gh on PATH.
GH_TOKEN="$(cat "${GH_TOKEN_FILE:-/github-auth/token}" 2>/dev/null)" exec /usr/bin/gh "$@"
