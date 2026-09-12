---
status: accepted (amends 0022 sequencing; renumbered from 0044 - main landed its own 0044..0050 mid-flight)
date: 2026-09-11
---

# 0051. Spend log record contract

## Context and problem statement

[0022](0022-cost-metering-and-model-routing.md) decided per-call spend
logging to a durable host file, rolled up at read time (decision 2), and
deferred the exact record format to the metering implementation (#46) as
`TODO(verify)`. #47 (`veggies costs`, the reader) landed first, while the
writer (#46) is still open: without a shared shape, reader and writer
would each grow their own guess and drift. Both sides need one stable
contract; this ADR pins it.

**This amends 0022 decision 4's implementation sequence**: metering (#46)
-> `veggies costs` (#47) becomes contract (this ADR) -> reader (#47) ->
writer (#46). Justification: contract-first converts #46's open
`TODO(verify)`s into "normalize litellm's export to this shape", and #47's
missing-log degradation criterion (decision 3 below) keeps the reader
correct standalone while no writer exists.

## Decision

1. **Location.** `<state_root>/<stack>/spend.jsonl`, where state_root is
   `~/.local/state/veggies` local or `/home/stacks/.local/state/veggies`
   remote (0022 decision 2). Rotated segments are plain-text
   `spend.jsonl.<N>`; compression is forbidden - a `.gz` segment would
   parse as malformed lines and silently shrink money reports. Readers
   glob `spend.jsonl*` and sort records by `ts`.
2. **Record.** One JSON object per line (JSONL). Required keys:
   - `ts` - epoch seconds, int or float. The single canonical timestamp
     type; the writer normalizes whatever litellm emits.
   - `model` - router alias string.
   - `prompt_tokens` - int.
   - `completion_tokens` - int.
   - `spend` - USD float.
   - `session` - the stamped session title (0022 decision 3); empty
     string or missing = unattributed.
   - `session_id` - string, REQUIRED. Re-kicks mint new sessions under
     the same `#N: <issue>` title (0034/0035) and supervisor refinements
     re-run the same session (0036), so title alone cannot separate
     them; the kick prints `SESSION_ID=` (`scripts/stack_kick.py`) and
     both judge paths know the judged session's id.

   Optional key: `attr` - `stamped` (default when absent) or `inferred`
   (0022's timestamp-window fallback; readers show the inferred share so
   reduced confidence is visible). Unknown extra keys are ignored by
   readers (litellm payload fields may ride along).
3. **Reader policies.** Malformed lines are skipped and counted, and the
   count is printed whenever > 0 - a money report never silently
   shrinks. A record with missing/non-numeric `spend` is kept, excluded
   from sums, rendered `?`, and counted as `unpriced` (a fallback model
   missing a price entry must not look free). A missing log file
   degrades to a clear message, never a traceback.
4. **Stamping gate.** The `session` stamp is 0022's open `TODO(verify)`
   on litellm `v1.99.1` / opencode 1.18.27. If stamping fails
   verification, #46 emits the timestamp-window fallback
   (`attr: inferred`) or, worst case, records degrade to the
   unattributed bucket BY DESIGN - downgraded confidence, never a
   fabricated per-PR number. The fix for a failed stamp belongs in #46's
   writer, never in weakening this attribution axis.

## Consequences

- Positive: reader and writer can be developed in any order; the
  contract is the single normalization point, so every future reader
  stays dumb; uncompressed segments keep rotation honest.
- Negative / accepted: a writer change is a contract change requiring an
  ADR amendment; epoch-only `ts` pushes the normalization cost onto
  #46's writer.

## Links

- Parent: [0022](0022-cost-metering-and-model-routing.md) (this ADR
  amends its decision 4 sequence). Title/session conventions:
  [0034](0034-session-observability.md),
  [0035](0035-one-shot-labels-and-done-guard.md),
  [0036](0036-always-on-critic-for-kicked-sessions.md). `D#N` discussion
  titles: [0038](0038-discussion-triggered-issue-distillation.md),
  [0041](0041-discussion-elaboration-persona-roster.md).
- Issues: #46 (writer), #47 (reader).
