---
status: accepted
date: 2026-09-11
---

# 0054. Per-repo harness overlay: one veggies.yml key, built from the pinned base at up-time

(Numbered 0054 because 0053 was contested by in-flight PRs at writing time; per the 0051 precedent this may be renumbered if one of those lands first.)

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
   user-overridable. `validate_overlay_containerfile` enforces: exactly
   one FROM (the instruction keyword parses case-insensitively) whose
   reference must equal the pinned base byte-for-byte, so `AS` stage
   names, `--platform` flags and any second FROM (multi-stage) all fail
   the equality check. COPY and ADD are rejected with an error that
   teaches the pinned-fetch pattern: fetch a version+sha256-pinned URL
   in a RUN step instead.
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
5. **Substrate-proxy builds, base first.** Remote builds ride the
   substrate squid proxy per
   [0016](0016-substrate-vs-stack-boundary.md) (proxy env on the podman
   call, `--network=host` plus proxy build-args so RUN steps reach it),
   and the overlay builds after the component images, so its FROM base
   already exists.
6. **Resolved ref on the StackSpec, never persisted.** The resolved ref
   rides `StackSpec.harness_image` - the same veggies.yml-key-to-spec
   path as `model`/`github` - but unlike them it is NOT written to
   state.json: it is re-resolved from the checkout on every
   `up`/`sync`/`render` (`render` resolves silently). Absent the key the
   render is byte-identical to before (golden-pinned).

**Experimental until #68.** `HARNESS_BASE_IMAGE` today names the full
derived image (`localhost/veggies-opencode:1.18.27`, the `IMAGE_OPENCODE`
alias). The base/derived split (#68) flips the constant to the slim base
image; every overlay then rebuilds against it at the next `up` - loudly
(missing tools, failed builds), never silently.

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
archives and the GitHub Actions blob-storage suffix). Anything beyond
it - e.g. crates.io - is issue #70, a hard follow-up: only remote
builds are proxied, so an overlay that builds locally can still 403 on
the VPS until then.

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
- Positive: when #68 lands, this repo's own 0032 dogfood toolchain
  becomes such an overlay - the reference implementation, and the end of
  every stack carrying this repo's tools.
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
  [0045](0045-repo-declared-verify-gate.md)
- Discussion #64; issues #68 (base/derived split), #69 (this ADR's
  implementation), #70 (build-time egress follow-up)
