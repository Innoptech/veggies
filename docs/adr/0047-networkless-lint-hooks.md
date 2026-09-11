---
status: accepted
date: 2026-09-11
---

# 0047. Networkless gitleaks/actionlint hooks: image-baked pinned binaries, language: system

## Context and problem statement

Issue #48 (out of discussion #39): kicked agents burned tool calls on a
merge gate that structurally cannot run in-pod. Two pre-commit hooks
needed what the pod does not have:

- The upstream `gitleaks` hook is `language: golang` - pre-commit builds
  gitleaks from source at hook-install time. Verified root cause
  (2026-09-11, in-pod): metadata fetches against `proxy.golang.org`
  succeed, but module zips redirect to signed
  `storage.googleapis.com/proxy-golang-org-prod/...` URLs - a fifth
  domain, off the squid allowlist, so the zip payloads 403.
- `actionlint-docker` needs a docker daemon, which
  [0028](0028-retire-canvas-own-the-critic-loop.md) bans from the pod;
  in-pod runs carried `SKIP=actionlint-docker`.

## Decision drivers

- AGENTS.md rule 3 makes `mask ci` the merge gate; a gate that cannot
  run where the agent runs is not a gate.
- The [0006](0006-egress-allowlist.md) egress posture: allowlist narrow
  domains, never storage continents.
- [0032](0032-dev-toolchain-in-harness-image.md) discipline: tools are
  image-baked, pinned by version AND sha256.

## Decision

Options considered:

A. Allowlist `storage.googleapis.com` - rejected: the domain fronts all
   of GCS; widening egress to a cloud-storage continent for a lint hook
   mortgages the 0006 posture.
B. Upstream `gitleaks-system` / `actionlint-system` hook ids - rejected:
   pre-commit still clones the hook repos over the network at install
   time.
C. **Chosen:** `repo: local` + `language: system` hooks over image-baked
   binaries, pinned by version AND sha256 (the `tofu-fmt` precedent).
   CI (`.github/workflows/infra-ci.yml` env) and `mask setup` install
   the same pinned pair; `tests/test_tool_pins.py` binds the three pin
   sites. Hook entries are identical to the upstream v8.30.1 / v1.7.12
   hooks except `language` (the description fields and upstream's
   `minimum_pre_commit_version` are dropped), so behavior is unchanged.

The staged-scan boundary, stated plainly: `gitleaks git --pre-commit
--staged` scans the git index. At commit time on a dev host that is
exactly the incoming change - its designed use. In CI and in the agent
flow (commit, then `mask ci`) the index is empty and the hook is
vacuously green there - as it already was with the golang hook, so no
behavior change. Automation's secrets net is the `vault-encrypted` hook
over `secrets/`; CI's gitleaks is defense in depth for human-host
commits. A PR-range or full-history gitleaks scan in CI is a possible
follow-up, deliberately out of scope here.

End state: the direction is a fully hermetic in-pod `mask ci`. After
this change only the python hooks (yamllint, ansible-lint,
pre-commit-hooks) still touch the network, and only on a cold pre-commit
cache - pypi is allowlisted and the same tools are pip-pinned in the
image. Their conversion is the natural follow-up.

## Consequences

- Positive: plain `mask ci` is the in-pod gate again (the declared
  verify-gate marker in AGENTS.md rule 3, rendered into the kick prompt
  by [0045](0045-repo-declared-verify-gate.md), says so); hook time
  involves zero network fetches.
- Negative / accepted: the image grows ~27 MB (the layer stores the
  uncompressed binaries). Hook id
  `actionlint-docker` -> `actionlint`; a stale `SKIP=actionlint-docker`
  becomes a harmless no-op (pre-commit ignores unknown SKIP ids).
- Rollout ordering: merging lands the hooks + the declared gate
  instantly (kicks branch off `origin/main` and the marker lives in
  AGENTS.md), but the baked binaries exist in-pod only after the
  operator rebuilds the image (`veggies prepare` / `up`) - do that
  before the next `agent-task` label. On hosts, re-run `mask setup`
  after pulling -
  setup now installs the four pinned binaries into `~/.local/bin`, and
  `mask ci`'s local hooks fail until it runs.
- Amends [0032](0032-dev-toolchain-in-harness-image.md): its two
  documented `mask ci` exclusions shrink to one (molecule).

## Links

- Amends: [0032](0032-dev-toolchain-in-harness-image.md)
- Related: [0028](0028-retire-canvas-own-the-critic-loop.md) (docker /
  podman socket ban), [0006](0006-egress-allowlist.md) (egress posture),
  [0045](0045-repo-declared-verify-gate.md) (the verify-gate marker this
  change shrinks to `mask ci`)
- Issue #48, discussion #39
