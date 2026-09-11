---
status: accepted (amended by 0046)
date: 2026-09-11
---

# 0034. Session observability: titled sessions, issue feedback, `veggies ui`

## Context and problem statement

With issue-triggered kicks live (0033), the watch story had three holes:
kicked sessions were anonymous in the web UI (`New session`), the issue
gave no sign of life, and reaching the UI meant remembering the
`ssh -L` incantation (which silently binds the wrong stack when a local
stack holds the same port - verified 2026-09-10).

## Decision drivers

- An issue is where people look: it should say which session is working it
  and how to watch, without ever exposing the serve password.
- The UI's own deep-link route (`/session/:id`) makes links useful once
  tunneled.
- Tailscale remains deferred (0024), so access helpers ride ssh; no new
  inbound listeners on the VPS.
- Comment permissions must not come from the bot PAT: the workflow's own
  GITHUB_TOKEN carries `issues: write`.

## Decision

- `stack_kick.py` titles every kicked session `#<n>: <issue title>`; the
  web UI list then reads like an issue list. Comment-triggered kicks append
  the triggering comment to the prompt. The kick prints `SESSION_ID=` and
  appends to `$GITHUB_OUTPUT`.
- The workflow comments on the issue after every kick (success: session
  id + deep link + attach instructions; failure: where to look). The
  password is never posted - only the local `jq` one-liner that reads it.
- New CLI surface: `veggies ui <name>` (local stacks print the URL; remote
  stacks hold a background `ssh -N -L` tunnel with a pidfile, reusing a
  live one; `--stop` closes; default local port = stack port + 1000) and
  `veggies sessions <name> [--issue N]` (terminal table: id, title,
  busy/idle, updated; the issue filter keys on the title prefix, not on
  session metadata, which the list API does not return).

Rejected: storing the issue in session `metadata` for API-side filtering
(the list endpoint does not return it, verified against 1.18.27) - the
title convention is both human- and machine-readable. Rejected: exposing
the UI through a public reverse proxy - inbound listeners stay out of
posture (0024).

## Consequences

- The demo/operate path is: issue gets labeled -> bot comments with a link
  -> `veggies ui veggie` -> click -> watch the titled session live.
- Multiple kicks per issue produce multiple titled sessions; the comment
  trail is the per-issue session log.
- Tunnel lifetime is the operator's shell session; the pidfile is small
  local state, safe to delete.

## Links

- Builds on: [0033](0033-issue-triggered-agent-kicks.md),
  [0024](0024-interim-access-and-identity-constraints.md)
