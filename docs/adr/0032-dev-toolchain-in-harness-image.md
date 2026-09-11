---
status: accepted (amended by 0047)
date: 2026-09-10
---

# 0032. Dev toolchain baked into the harness image

## Context and problem statement

Dogfooding means the in-stack agent works this very repo, and AGENTS.md
makes `mask ci` the merge gate. The opencode image shipped only
`git`/`gh`: no python, mask, ansible, or tofu - so an agent inside a stack
could not run the repo's own checks (verified 2026-09-10, first dogfood
session). The runtime rootfs is read-only (`HARDENED`), so nothing can be
installed after the fact; tools must be baked in.

## Decision drivers

- The agent must be able to satisfy AGENTS.md rule 3 (`mask ci`) inside
  the stack, unattended.
- Supply-chain discipline of the base image (tag+digest pin) extends to
  anything we add.
- All fetches must pass the squid allowlists, in-pod and substrate.

## Decision

`deploy/images/opencode.Containerfile` gains: `bash curl unzip python3
py3-pip` (apk); `ansible-core`, `ansible-lint`, `pre-commit`, `yamllint`,
`pytest` (pip, pinned to `requirements-dev.txt`); and the static binaries
`mask` (musl build), `tofu`, `tflint` from GitHub releases, each pinned by
version AND sha256. `registry.opentofu.org` joins the squid base allowlist
(both copies - the drift test pins them equal) so `tofu init` works
in-container.

Deliberately excluded:

- **molecule** - needs a podman socket; the 0028 history documents why
  that socket stays out of the pod. `mask molecule-*` is host-only.
- The `actionlint-docker` pre-commit hook needs a docker daemon; in-container
  runs use `SKIP=actionlint-docker` (CI on GitHub-hosted runners still
  covers it).
- In-container checks are a clone-stack story: in a mount-mode stack the
  mounted `.venv` is the HOST's and shadows the image's tools on the
  maskfile's PATH (verified 2026-09-10: foreign-venv shebangs partially
  resolve under alpine's python and fail confusingly). The kick prompt
  tells agents not to build a venv; the runbook carries the boundary.

## Consequences

- Image grows by ~400 MB (tofu and tflint are the bulk); builds ride the
  substrate proxy on the VPS and take minutes, cached afterwards.
- `mask ci` runs green in-container except the two documented exclusions.
- Bumping a tool = version+sha256 edit in the Containerfile; no tag bump
  needed (the image tag tracks the opencode version; `ensure_images`
  rebuilds on every `up`).

## Links

- Related: [0028](0028-retire-canvas-own-the-critic-loop.md) (podman
  socket ban), [0030](0030-opt-in-github-write-credentials-in-stacks.md)
