---
status: accepted
date: 2026-09-11
---

# 0047. Spend-log mechanism: litellm custom callback, title stamping, and the record contract

## Context and problem statement

[0022](0022-cost-metering-and-model-routing.md) decided WHAT: meter at
the in-pod litellm router, export per-call records to a durable host
file, attribute via the session-title convention. It left the exact
mechanism open as `TODO(verify)`: the per-call export hook on the pinned
`v1.99.1` image, the metadata/tags field that reaches the exported
records, and the opencode harness's per-request stamping capability.
Issue #46 resolved all three; this ADR records the mechanism as built.

Upstream docs were egress-blocked while this was built, so verification
ran against the pinned artifacts themselves: the litellm **1.99.0** wheel
source (PyPI carries no 1.99.1; the image pins the `v1.99.1` tag - the
patch delta is carried as residual risk) and the opencode **1.18.27**
binary's plugin surface. What could not be verified without a live pod is
listed under Consequences, not assumed.

## Decision

1. **Export: a custom callback, not a spend DB.**
   `agent-config/litellm/config.yaml` wires
   `litellm_settings.callbacks: ["custom_callbacks.proxy_handler_instance"]`
   (list form: the string form would *replace* `litellm.callbacks`,
   evicting the proxy's built-in budget limiter; the list extends it);
   litellm imports the module from the config file's own directory (the
   CLI ships `custom_callbacks.py` next to `config.yaml` on remote
   stacks; locally the vendored `agent-config/litellm/` is live-mounted).
   Only the async success/failure event methods are implemented - the
   proxy path is async, and streamed calls arrive assembled with usage.
   This is the same event path that feeds litellm's in-memory `/spend`,
   which keeps working (`proxy_logging_obj` is wired separately); the
   host file stays authoritative per 0022. The log path defaults to
   `/costs/costs.jsonl` (`VEGGIES_COST_LOG` overrides; nothing in the
   deployment sets it) on the writable `costs` hostPath; `VEGGIES_STACK`
   stamps each line's `stack` key.
2. **The record contract** (what #47's `veggies costs` reader codes
   against): one JSON object per line, keys `v, ts, call_id, stack,
   status, caller, model, model_group, prompt_tokens, completion_tokens,
   total_tokens, spend, session_id, session_title, key_alias, tags`.
   - `v` is the schema version (1); bump it on any contract change.
   - `ts` is UTC ISO-8601 (milliseconds, `+00:00`). `status` is
     `success` or `failure`. `model` is the provider model id,
     `model_group` the litellm alias.
   - `spend` is USD as litellm's price map computes it - an estimate.
     Unpriced/unknown models record `spend: null` on a success line; `0`
     means priced-at-zero; raw token counts are on every success line, so
     spend can be repriced at read time.
   - `session_title` precedence: request-body `metadata.session_title`,
     then the URI-decoded `session-title:` tag, then null. `caller` and
     `session_id` follow the same body-metadata > tag > null shape
     (their tags are not URI-decoded).
   - `key_alias` is null until per-agent virtual keys land (deferred in
     0022); the key is in the contract now so the format pre-pays that
     future.
   - Failures add `error` (`<ExceptionClass>: <message>` truncated to
     200 chars) with usage/spend null; success records have no `error`
     key. `tags` carries the raw metadata tags list.
   - One record per litellm logging event = settled, billable outcomes.
     In-flight streams at `veggies down` are lost; router-internal retry
     granularity (`num_retries: 2`) is `TODO(verify)` on a live stack. A
     mid-stream disconnect is logged as a failure with null usage even
     though litellm recovers partial usage internally
     (`combined_usage_object`) - accepted undercount; repricing or
     revisiting belongs to #47.
3. **Attribution stamps at the two producer sides.**
   - opencode: `agent-config/plugins/metering.js` hooks `chat.headers`
     and comma-appends to the `x-litellm-tags` header:
     `caller:opencode`, `session-id:<id>`, `session-title:<uri-encoded>`.
     The title is the ROOT session's - task subagents run in child
     sessions, so the plugin walks `parentID` links upward (max 8 hops).
      Resolutions are cached positive-only: manual sessions get
      auto-titled after their first call, and a cached miss would keep
      them unstamped forever. When no title resolves, the title tag is
      skipped but caller + session-id still stamp - the record stays
      joinable. The title tag is also skipped when its encoded length
      exceeds 1024 chars: an unbounded title would become an unbounded
      header and kill the call at the HTTP layer, outside the hook's
      fail-open.
   - Both judge paths stamp request-body metadata instead:
     `caller: veggies-supervise` (`veggies supervise`, exec inside the
     litellm container, 0028) and `caller: supervisor-daemon` (the in-pod
     supervisor sidecar, 0036), each with the judged session's
     `session_title` / `session_id` (keys omitted when empty).
   - The title is untrusted issue-title content: consumers treat it as
     data, never shell.
4. **Rotation owner: the writer process.** Stdlib `RotatingFileHandler`,
   10 MiB x 5 segments (~60 MiB cap with the active file). Deliberate
   deviation from issue #46's "host's standard mechanism" (logrotate): a
   logrotate drop-in is root-owned substrate (ansible territory per
   0016), needs copytruncate against an open fd, and does nothing for
   local-workstation stacks; 0022 mandated size-based rotation with no
   ansible involvement. Correctness assumes a single writer process in
   the litellm container; scaling workers re-opens this. Rotation is
   silent deletion past the cap: the long-period record rides restic
   (`backup_paths` already covers the directory) once 0024 un-gates.
5. **Fail-open invariant.** The callback never raises into the request
   path: an unwritable mount or full disk degrades to stderr lines in
   `podman logs` (a loud `COST METERING DISABLED: ...` at handler-open,
   one `dropping record` line per failed append), never to failed or
   slowed model calls. Startup logs the active path
   (`cost metering: appending to /costs/costs.jsonl (10MiB x 5)`), and
   the container sets `PYTHONDONTWRITEBYTECODE=1` because the callback
   imports from the readOnly `/agent-config` mount.
6. **Always-on ratified** (0022 decision 1): no `veggies.yml` opt-in - a
   metering toggle is a permanent config dimension that exists only to
   create attribution holes.
7. **State-scope answer.** This answers 0021's open scope question
   ("confirm nothing else accretes state") for one more file:
   `costs.jsonl` is new durable stack state, already inside the backup
   role's `backup_paths` (`/home/stacks/.local/state/veggies`).

## Consequences

- Positive: durable across `down` / `up` / `sync`; zero new processes,
  listeners, or egress destinations; per-PR attribution falls out of the
  title convention (0034) with an explicit unattributed remainder; the
  format pre-pays the virtual-keys future; writer-side rotation needs no
  root-owned substrate.
- Negative / accepted: rollup-at-read cost grows with log size (bounded
  by the ~60 MiB cap); rotation silently drops the oldest segments past
  the cap, so long-period history waits on the 0024 backup re-entry;
  `down --purge` deletes cost history (the CLI warns first); local
  stacks have no backup at all.
- Pending live verification (`TODO(verify)`, to record on #46 at the
  first post-merge `veggies up` - the kick environment had no
  podman/docker, so none of this ran end to end):
  - the litellm container can write the hostPath under rootless UID
    mapping;
  - `spend > 0` on the priced Fireworks aliases;
  - whether compaction/title-gen calls traverse `chat.headers` (if they
    bypass the hook they land in the unattributed bucket);
  - retry granularity: one record per settled call vs per router
    attempt.

## Links

- Implements [0022](0022-cost-metering-and-model-routing.md) (its
  mechanism TODOs) - issue #46; the reader lands in #47 (`veggies
  costs`). Demand signal: discussion
  <https://github.com/Innoptech/veggies/discussions/38>.
- Router and mount ownership: [0011](0011-litellm-gateway-model-routing.md),
  [0016](0016-substrate-vs-stack-boundary.md); state scope:
  [0021](0021-stack-data-backup-and-restore.md); backup gate:
  [0024](0024-interim-access-and-identity-constraints.md); judge paths:
  [0028](0028-retire-canvas-own-the-critic-loop.md),
  [0036](0036-always-on-critic-for-kicked-sessions.md); the title
  convention the stamps ride: [0034](0034-session-observability.md).
