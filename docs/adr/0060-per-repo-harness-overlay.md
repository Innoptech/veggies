---
status: accepted
date: 2026-09-14
---

# 0060. Per-repo harness overlay: one veggies.yml key, built from the pinned base at up-time

(Drafted as 0054; main landed 0053..0059 mid-flight, so it renumbers
here per the 0051/0057/0059 renumber precedent.)

## Context and problem statement

Discussion #64 separated three shapes of "repo tooling". Two already have
homes: agent-shaped tools are per-repo files under `.opencode/`,
discovered with zero config
([0019](0019-agent-rosters-and-skills.md)); service-shaped tools are
opt-in `mcps:` sidecars ([0018](0018-mcp-server-components.md)). The gap
is binary-shaped tools: the repo's own check toolchain. The
repo-declared verify gate
([0045](0045-repo-declared-verify-gate.md)) forces that toolchain to run
in-pod, and the harness pod's rootfs is read-only
([0032](0032-dev-toolchain-in-harness-image.md)), so nothing installs at
runtime. Until now every stack carried this infra repo's 0032 dogfood
toolchain and no other repo could run its own gate in-pod.

All five thread persona reviews rejected a `tools:` package list and
runtime installs; "Alternatives rejected" below records why.

## Decision

1. **One veggies.yml key.** `harness_containerfile: <repo-relative
   path>` joins schema v1. Validated at parse time
   (`validate_overlay_path` in `cli/veggies_stack.py`): must be a
   string, repo-relative, never absolute, never escaping the repo via
   `..`.
2. **FROM-pin against the CLI-owned base.** The overlay Containerfile
   must be `FROM` exactly `HARNESS_BASE_IMAGE` in
   `cli/components/opencode.py` - one constant, infra-owned, never
   user-overridable. The constant names the SLIM base
   (`localhost/veggies-opencode-base:<ver>`): a repo overlay joins
   [0057](0057-harness-base-image-and-repo-overlay.md)'s pin chain at
   the same link this repo's own toolchain overlay uses - the base pins
   the upstream image by tag+digest, while overlays (this repo's and
   foreign repos' alike) pin the base by tag, because a locally-built
   image has no stable digest to pin.
   `validate_overlay_containerfile` enforces: exactly
   one FROM (the instruction keyword parses case-insensitively) whose
   reference must equal the pinned base byte-for-byte, so `AS` stage
   names, `--platform` flags and any second FROM (multi-stage) all fail
   the equality check. COPY and ADD are rejected with an error that
   teaches the pinned-fetch pattern: fetch a version+sha256-pinned URL
   in a RUN step instead. The validator's line handling mirrors
   imagebuilder's parsing - a leading BOM is stripped, comment lines
   are stripped before continuation joining, continuations join by
   direct concatenation - and it REJECTS `# escape=` parser directives
   outright rather than porting directive semantics: strict by
   construction, relaxable later (the same backward-compat argument as
   the FROM strictness).
3. **Content-addressed tag, unconditional rebuild.** The overlay image
   is `localhost/veggies-harness-overlay:<sha256(containerfile
   text)[:16]>` - the tag names overlay content only. `ensure_images`
   rebuilds it on EVERY `up`/`prepare`: never add an existence check or
   a `--layers` fast path. A `HARNESS_BASE_IMAGE` flip busts the layer
   cache via buildah's parent-image-ID cache keying, and the rebuilt
   image re-takes the same tag.
4. **Repo-empty build context.** The build context is the state images
   dir, not the repo: no repo contents are ever shipped into a (possibly
   remote) build, and the toolchain layer caches until the Containerfile
   itself changes, because nothing else can reach it. This is also why
   COPY/ADD have nothing to copy from.
5. **Substrate-proxy builds, base first, derived image skipped.** Remote
   builds ride the substrate squid proxy per
   [0016](0016-substrate-vs-stack-boundary.md) (proxy env on the podman
   call, `--network=host` plus proxy build-args so RUN steps reach it),
   and the overlay builds after the component images, so its FROM base
   already exists. When the key is set, the CLI builds the base and the
   repo overlay and SKIPS this repo's derived toolchain image
   (`veggies-opencode`) for that stack - a foreign repo never pays the
   ~400 MB dogfood tax 0057 removed from the shared harness.
6. **Resolved ref on the StackSpec, never persisted.** The resolved ref
   rides `StackSpec.harness_image` - the same veggies.yml-key-to-spec
   path as `model`/`github` - but unlike them it is NOT written to
   state.json: it is re-resolved from the checkout on every
   `up`/`sync`/`render` (`render` resolves silently). Absent the key the
   render is byte-identical to before (golden-pinned).

**Landed substrate (0057).** This key was drafted before the
base/derived split; issue #68 /
[0057](0057-harness-base-image-and-repo-overlay.md) has since shipped it
- the slim base is every stack's harness, this repo's 0032 toolchain
moved into the derived overlay, and 0057 explicitly deferred the
per-repo generalization to #69. This key is that generalization.

### Trust posture

A repo-built harness image is no privilege escalation:
[0019](0019-agent-rosters-and-skills.md) already hands the mounted repo
bash inside the pod. But overlays are a NEW build-time supply-chain
surface, so the rules are: pinned fetches only (version + sha256, the
0032 pattern), proxied, and reviewed in PRs like everything else
versioned with the repo. The v1 fetch envelope is the substrate squid
allowlist (`ansible/roles/egress/defaults/main.yml`): github.com + its
release CDNs, dl-cdn.alpinelinux.org, pypi.org +
files.pythonhosted.org, registry.npmjs.org, go.dev + dl.google.com +
proxy.golang.org + sum.golang.org, registry.opentofu.org, docker hub +
ghcr (the file is the source of truth; it also carries the ubuntu
archives and the GitHub Actions blob-storage suffix). Since
[0058](0058-build-time-egress-legible-denials.md) (issue #70, closed) a
fetch outside the envelope fails the remote build with the blocked
domain NAMED and a paste-ready remedy; widening the envelope is a
normal substrate PR (`egress_allowlist_base` /
`egress_allowlist_extra`), not a gate. Local builds are unproxied, so a
fetch that works locally can still be denied on the VPS - and the 0058
error names the domain when it is. 0057 recorded the why: overlay RUN
steps are arbitrary build-time code running as the stacks user with
`--network=host` on the VPS, so other stacks' published ports are
reachable from the build netns (passwords bound the damage) - that is
why the rule is pinned fetches reviewed in PRs, not "the repo asked
nicely".

### Session isolation (0037)

The veggies.yml read happens from the shared checkout at up-time, and
the clone only advances on `veggies sync`
([0037](0037-per-session-worktrees.md)). A kicked session working in its
own worktree can therefore never re-tool its own running pod: an overlay
edit it makes takes effect only at the next operator-driven up/sync.

## Alternatives rejected

- **`tools:` package-list DSL.** A worse Containerfile: version pinning
  and PR reviewability reinvented badly, and runtime package
  installation collides with the read-only rootfs. Deferred permanently -
  the Containerfile IS the DSL.
- **Runtime installation.** Hard no: the rootfs is read-only on purpose
  (0032), and per-boot installs are unauditable and unreproducible.
- **`image:` / `harness_image:` pull-an-image key.** Abandons the
  pinned-base contract - a repo would pull an arbitrary, unaudited
  image instead of layering onto the audited base.
- **Convention-only path** (a magic `.veggies/harness.Containerfile`
  with no key). Invisible configuration; the key makes the overlay
  reviewable in one grep.
- **Unprefixed `containerfile:`.** Ambiguous which container it builds.
- **Multi-stage FROM / COPY in v1.** Relaxing a validator is
  backward-compatible; tightening never is - v1 starts strict.

## Consequences

- Positive: repos ship their check toolchain versioned with themselves -
  the 0045 gate a repo declares can actually run in-pod. Stacks without
  the key are untouched: zero migration, byte-identical renders.
- Positive: this repo's own 0032 dogfood toolchain already IS such an
  overlay - 0057's reference implementation - and with the key set a
  foreign repo's stack skips that derived image entirely.
- Positive / namespacing: the content-hash tag IS the per-repo namespace
  0057 demands of #69 - different overlay content never shares a tag
  (two repos' overlays cannot thrash each other on one host), and
  identical content legitimately shares one image (the layer-dedup
  promise). `up` prints the resolved ref with the repo-relative
  Containerfile path (the `overlay:` line) and `prepare` prints the tag
  in its build header, so an image in the host store is attributable to
  its source.
- 0059 interaction: the kick-time tool-pin gate
  ([0059](0059-kick-time-tool-pin-gate.md)) reads the kicked repo's
  checkout for infra's `deploy/images/opencode.Containerfile`; an
  overlay repo carries none, so the gate degrades loud-to-proceed (a
  stderr note), never false-refuses. The container-start manifest
  publish is likewise tolerant of overlay images carrying no
  `/etc/veggies/tool-pins` (rm-first, missing-file-tolerant). Per-repo
   pin attestation is 0059's named future opt-in (pins declared in the
   kicked checkout, the way 0045's marker declares the verify gate). One
   combination stays unreachable today and must stay so: a checkout
   carrying BOTH infra's `deploy/images/opencode.Containerfile` AND the
   `harness_containerfile` key would be refused by the gate with a
   rebuild remedy that cannot apply (the key skips the derived build),
   so this repo must not adopt its own key without the overlay baking a
   matching manifest; adopted repos cannot reach it - the kick delivery
   ships only the workflow and the script, no Containerfile.
- Boundary carried from 0057: the kick prompt's "tools preinstalled"
  line describes THIS repo's dogfood image - on an overlay stack the
  real toolchain is the repo's overlay plus its AGENTS.md, and per-repo
  prompt output stays future work.
- Negative / accepted: the shadowing trap generalizes. Anything
  repo-local on PATH or in the environment - a mounted `.venv`, mise,
  `.tool-versions`, go.mod toolchain directives - shadows the overlay's
  tools, and verification then runs the WRONG environment silently.
  `up` warns on stderr when an overlay meets a repo `.venv`; the rest is
  operator discipline (the runbook carries the boundary).

## Links

- Related: [0016](0016-substrate-vs-stack-boundary.md),
  [0018](0018-mcp-server-components.md),
  [0019](0019-agent-rosters-and-skills.md),
  [0032](0032-dev-toolchain-in-harness-image.md),
  [0037](0037-per-session-worktrees.md),
  [0045](0045-repo-declared-verify-gate.md),
  [0057](0057-harness-base-image-and-repo-overlay.md) (the base/overlay
  split; this repo's own toolchain overlay is the reference
  implementation),
  [0058](0058-build-time-egress-legible-denials.md) (legible build-time
  egress denials),
  [0059](0059-kick-time-tool-pin-gate.md) (the kick-time tool-pin gate)
- Discussion #64; issues #68 (landed: the split), #69 (this ADR's
  implementation)
