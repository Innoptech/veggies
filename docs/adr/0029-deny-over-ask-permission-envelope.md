---
status: accepted (amended by 0031)
date: 2026-09-09
---

# 0029. Deny-over-ask permission envelope for unattended sessions

## Context and problem statement

Stacks increasingly run sessions nobody is watching (supervise'd runs,
automations, `prompt_async` API kicks). opencode's permission model has
three actions - allow / ask / deny - and `ask` in an unattended session
means *park forever*: verified 2026-09-09, a headless session sat 25
minutes on an `external_directory` prompt with zero progress and no
signal. (`doom_loop` and `external_directory` are the only permissions
defaulting to `ask`.)

## Decision drivers

- Unattended sessions must never park silently.
- Denied actions return an error immediately and the agent routes around
  them (verified: the same session continued after a reject).
- The stack is disposable (ephemeral pod, workspace is a clone or a
  git-tracked mount, egress allowlisted), so in-workspace autonomy is
  cheap; out-of-workspace access and un-undoable commands are not.
- ADR 0024's interim posture already trades guardrails for velocity; the
  PR flow (branch protection, checks) is the real gate.

## Decision

The vendored `agent-config/opencode.json` carries an explicit envelope:
`*: allow` baseline; `read` keeps the built-in `.env` deny and adds
`secrets/*.yml` (vault ciphertext never enters a transcript); `bash`
allows everything except the three un-undoable `rm -rf` shapes (`/`,
`~`, `$HOME`); `external_directory` denies everything except `/tmp/**`;
`doom_loop` denies (three identical calls is definitionally stuck);
`skill` stays allow. Per-agent frontmatter overrides still take
precedence (opencode's documented merge order).

Rejected alternatives:

- **`opencode serve --auto`** (auto-approve anything not denied): the
  policy then lives in a runtime flag and approves whatever nobody
  remembered to deny. The config file *is* the policy - diffable,
  reviewable, test-covered.
- **Keeping `ask` for interactive use**: one opencode.json serves both
  TUI and API sessions; the envelope optimizes for the unsupervised case
  and attached users lose nothing they needed (they can still be asked
  nothing and see everything).

## Consequences

- Unattended sessions fail fast instead of parking; `veggies supervise`
  (0028) surfaces anything that still stalls.
- Widening = one pattern line, PR, re-up (runbook has the recipe).
- Residual park risk: the `question` tool stays at its default (an agent
  asking a blocking question in a headless session still waits);
  mitigated by prompt convention ("work autonomously"), revisited if it
  bites.
