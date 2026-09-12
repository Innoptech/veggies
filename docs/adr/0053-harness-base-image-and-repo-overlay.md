---
status: accepted
date: 2026-09-12
---

# 0053. Harness base image split from the repo toolchain overlay

## Context and problem statement

Discussion #64 / issue #68: the opencode image conflated two jobs. It was
the harness every stack runs (opencode serve, git, gh) AND this repo's
[0032](0032-dev-toolchain-in-harness-image.md) dogfood toolchain
(python/mask/ansible/tofu/tflint, plus gitleaks/actionlint per
[0047](0047-networkless-lint-hooks.md)) - a ~400 MB tax on every stack of
every repo, including repos whose agents never run `mask ci`. The shared
renderer also shipped infra glue to repos without ansible: the
ansible-vault dummy (`~/.config/infra/vault-password`) existed only to
satisfy THIS repo's ansible.cfg startup check, yet every stack's boot
script created it.

[0045](0045-repo-declared-verify-gate.md) had already named the seam and
deferred it: a foreign gate needs an image that can run it, and the repo
owner owns that toolchain story.

## Decision drivers

- The harness image should carry zero repo assumptions; a repo's
  concerns layer on top, never inside - the
  [0016](0016-substrate-vs-stack-boundary.md)/[0023](0023-capability-model-dependency-reversal.md)
  boundary applied to images.
- [0032](0032-dev-toolchain-in-harness-image.md)/[0047](0047-networkless-lint-hooks.md)
  pin discipline extends to the split itself: nothing unpinned enters
  either image.
- The per-repo mechanism must be a Containerfile FROM the base - a real
  build with real pins - never a runtime `packages:` list.

## Decision

Two images replace one.

- **Base** `localhost/veggies-opencode-base:1.18.27`
  (`deploy/images/opencode-base.Containerfile`): harness only - the
  official opencode image plus `git`, `openssh-client`, `github-cli`.
  git/gh are harness capability, not repo tooling
  ([0030](0030-opt-in-github-write-credentials-in-stacks.md)). This
  contents line is THE rule future overlays follow: anything
  repo-specific belongs in an overlay, never here.
- **Overlay** `localhost/veggies-opencode:1.18.27`
  (`deploy/images/opencode.Containerfile`): FROM the base, carrying this
  repo's
  [0032](0032-dev-toolchain-in-harness-image.md)/[0047](0047-networkless-lint-hooks.md)
  toolchain, the `TF_REGISTRY_CLIENT_TIMEOUT=120` ENV, and the ansible
  vault dummy. The overlay keeps the established name/tag, so every
  existing reference keeps working. Both tags track the upstream
  opencode version and bump in lockstep.

The pin chain, as the headline: **the digest pins the outside world, the
repo pins the inside.** The base pins the upstream image by tag+digest.
The overlay pins the base by tag only - a locally-built image has no
stable digest at author time, and both Containerfiles are versioned in
this repo and built in the same `ensure_images` pass, so the tag is the
contract. A pytest drift guard binds all four bump touchpoints (base
FROM tag+digest, `IMAGE_OPENCODE_BASE`, overlay FROM, `IMAGE_OPENCODE`):
`test_opencode_base_containerfile_pin_format` and
`test_opencode_overlay_is_from_pinned_base` in `tests/test_veggies.py`.

Wiring: `BuildSpec.base` (`cli/capabilities.py`) declares the chain -
single level only; a base must not itself carry a base. `ensure_images`
builds the declared base before the overlay, local and remote.

The vault dummy left the shared renderer and is baked into the overlay
image: `/etc/veggies/vault-password` plus `ENV
ANSIBLE_VAULT_PASSWORD_FILE=/etc/veggies/vault-password`. The env var
overrides ansible.cfg's `vault_password_file` (verified in-pod against
ansible-core 2.21: with the cfg path missing, `ansible-inventory -i
localhost, --list` exits 1; with the env pointing at the dummy, exit 0,
no vault errors). NOT baked under /root: the opencode-home volume mounts
over /root at runtime and would shadow it.

## Consequences

- Positive: the harness image carries zero repo assumptions - no
  ansible, no vault dummy, no toolchain on the slim base.
- Honesty bullet: until #69 lands, every stack still gets THIS repo's
  overlay - the component's `BuildSpec` is hardcoded, so the split
  changes nothing yet for foreign repos. The slim base alone cannot run
  this repo's `mask ci` gate (no python/bash/pre-commit). That gap is
  the explicit justification for #69's overlay key, which is a path to a
  repo-local Containerfile FROM the base - never a `packages:` list.
- #69 must namespace derived images per repo (e.g. `<repo>-opencode`):
  image names are host-local while stack names are global, so two repos'
  overlays sharing one tag would thrash each other on every `up`.
- Existing stacks roll over automatically at the next `up`/`prepare`:
  the overlay name/tag is unchanged, `ensure_images` rebuilds, and the
  layer cache makes it a no-op when nothing changed.
- The baked ENV overrides any mounted repo's own ansible.cfg
  `vault_password_file` - intended; real decryption in-pod was already
  impossible by design (the vault password never ships).
- Trust posture, recorded for #69/#70: a repo-supplied overlay
  Containerfile is arbitrary build-time code running as the stacks user
  with `--network=host` - other stacks' loopback endpoints are
  reachable, with egress only via the substrate proxy. This diff adds no
  such surface; the only overlay is this repo's own, versioned here.
- The mount-mode boundary carries forward from
  [0032](0032-dev-toolchain-in-harness-image.md): verifiable in-pod
  checks are a clone-stack story (a mounted `.venv` shadows the image's
  tools). The kick prompt's "tools preinstalled" line is clone-mode-true
  and becomes per-repo output at #69.
- Bump recipe: edit all four spots (both Containerfiles' FROM lines and
  `IMAGE_OPENCODE_BASE`/`IMAGE_OPENCODE` in
  `cli/components/opencode.py`) and re-verify the instruction-file
  discovery list
  ([0049](0049-no-ask-merged-roster-and-instruction-discovery.md))
  against the new upstream version - the drift guard fails otherwise.
- Rollout: image builds can't run in CI or in-pod
  ([0028](0028-retire-canvas-own-the-critic-loop.md)), so the first real
  build is the VPS's next `veggies prepare`/`up` - do it before the next
  `agent-task` label. Rollback = revert + rebuild; no state migration.

## Links

- Amends: [0032](0032-dev-toolchain-in-harness-image.md)
- Related: [0030](0030-opt-in-github-write-credentials-in-stacks.md)
  (git/gh as harness capability),
  [0045](0045-repo-declared-verify-gate.md) (deferred image
  generalization to the repo owner - landed here as the reference
  implementation), [0047](0047-networkless-lint-hooks.md),
  [0016](0016-substrate-vs-stack-boundary.md)/[0023](0023-capability-model-dependency-reversal.md)
- Issue #68, #69, #70; discussion #64
