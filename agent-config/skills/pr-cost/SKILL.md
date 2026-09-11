---
name: pr-cost
description: Answer "what did this PR cost?" or "what did issue N cost?" by running the host-side `veggies costs` command against the stack spend log and relaying its receipt verbatim
license: MIT
metadata:
  audience: agents
---

## What I do

- Answer per-PR / per-issue cost questions with the operator-side `veggies costs` CLI (#47) - never from memory, never by estimating tokens.
- Probe, resolve the stack, run one command, relay the output verbatim, attach provenance and blind spots every time.
- Degrade honestly: when the CLI is out of reach, hand the operator the exact host-side command. **Never invent a cost figure.**

## When to use me

When a human asks what a pull request or issue cost ("what did this PR cost?", "what did issue N cost?").

## Step 0: probe the environment

Run `command -v veggies` and `veggies ls` first. Inside a kicked session's pod both fail - no veggies CLI, no
`~/.local/state/veggies`, no spend-log mount: the opencode container mounts repo/stack-config/home/tmp only; the
litellm container alone mounts stack-state (ADR 0022 decision 4). On failure, answer with ONE honest step and stop:

    veggies costs <stack> --pr N      # run this on the host

Fill the stack name in from context when known, `<stack>` otherwise; use `--issue N` when the question names an
issue rather than a PR. Never invent a cost figure.

## Resolve the stack

Match `veggies ls`'s REPO column to the PR's repo.

- Zero matches: say no stack matched and stop. No matching stack is not a free PR.
- Multiple matches (laptop dev stack + VPS stack on the same repo): list the candidates and stop - the operator picks.
- One match: run the lookup against its NAME.

Never hand-roll ssh: the CLI does the remote read from the stack record itself.

## Run

    veggies costs <stack> --pr N

`--pr` resolves the PR's `agent/issue-M` branch via operator-side `gh pr view` and needs gh auth; when that fails,
read M from the PR's branch name and fall back to `veggies costs <stack> --issue M`.

## Relay, never re-derive

Report the output as printed: header total (`Issue #M: $X across N sessions (K calls)`), the SESSION_ID/CALLS/
TOKENS_IN/TOKENS_OUT/SPEND/TITLE cascade, per-title subtotal lines, the per-model table, the spend bars, and
evidence lines such as the unpriced count. Never re-sum numbers in prose. Read the cascade correctly:

- One row = one (session_id, title). The kick session's own row is its direct calls (main loop, CI retries);
  supervisor judge calls stamp the judged session's id + title, and refinements re-run the same session: both
  grow that row.
- Plan/persona refinement, task subagents and adversarial review run in child sessions: the metering plugin
  walks parentID upward for the TITLE but stamps the child's own session_id - separate same-titled rows, folded
  by the per-title subtotal and header total. Re-kicks mint new session_ids under the same title: also new rows.
- Several same-titled rows therefore do NOT prove a re-kick - subagent fan-out has the same shape - and no
  phase is separable beyond row granularity: never promise a per-phase breakdown.

## Provenance and blind spots (attach every time)

- Source: the host spend log `<state_root>/<stack>/spend.jsonl*` (`~/.local/state/veggies` local,
  `/home/stacks/.local/state/veggies` remote) - per-call JSONL from the in-pod litellm metering callback (record
  contract ADR 0051, writer ADR 0052), read by `veggies costs`.
- USD figures are litellm price-map estimates - operational, not an audit trail. `veggies down --purge` erases
  the log; local stacks have no backup.
- Work done before the metering writer landed (issue #46) left no records and is absent.
- Rotation keeps 10MiB x 5 segments; older history falls off.
- Unpriced models are excluded from totals, rendered `?`.
- Sessions titled off the `#N:` convention land in `(unattributed)`.

## Reply shape

Delivery into the PR thread is operator copy-paste - format for it: a paste-ready summary block (total, session
count, top model, one-line blind-spot footnote), then the relayed detail in a code block.

## Worked example

On PR #16 (branch `agent/issue-15`), "what did this change cost?":

1. Probe: `command -v veggies` + `veggies ls` - stack `veggie`'s REPO matches the PR's repo.
2. Run `veggies costs veggie --pr 16` (gh resolves `agent/issue-15`; on gh failure, `veggies costs veggie --issue 15`).
3. Reply:

   ```
   Cost of PR #16 (issue #15): $1.23 across 2 sessions, top model deepseek-v4.
   Estimate from the host spend log; pre-metering work and rotated-out history are absent.

   Issue #15: $1.23 across 2 sessions (41 calls)
   ... relayed cascade, subtotal, per-model table, bars ...
   ```
