# veggies opencode BASE image: the harness every stack shares (ADR 0053) -
# the official opencode image plus git, which the official image lacks
# (verified 2026-09-04: alpine-based, root, no git/node), plus gh for
# github-enabled stacks (ADR 0030): gh reads GH_TOKEN and api.github.com is
# already on the squid allowlist. Nothing repo-specific here: a repo's own
# toolchain layers on via an overlay Containerfile FROM this image - this
# repo's overlay is deploy/images/opencode.Containerfile.
# Upstream pinned by tag AND digest; bump both together with the overlay's
# FROM and IMAGE_OPENCODE_BASE/IMAGE_OPENCODE in cli/components/opencode.py
# (tests/test_veggies.py enforces the lockstep).
FROM ghcr.io/anomalyco/opencode:1.18.27@sha256:1eedcb5d4439130e35f5cf76d87c786c4eeb12dc7afebd79663f6c8341fa8505

# Build-time proxy args: on the VPS the stacks user is direct-egress-denied,
# so image builds must ride the filtering proxy. buildah exposes ARGs to RUN
# steps as env (both cases: apk/pip read lowercase, curl reads either).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG NO_PROXY=""

RUN apk add --no-cache git openssh-client github-cli
