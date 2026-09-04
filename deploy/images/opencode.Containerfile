# garden opencode image (ADR 0013): official image + git, which the official
# image lacks (verified 2026-09-04: alpine-based, root, no git/node).
# Base pinned by tag AND digest; bump both together.
FROM ghcr.io/anomalyco/opencode:1.18.27@sha256:1eedcb5d4439130e35f5cf76d87c786c4eeb12dc7afebd79663f6c8341fa8505

RUN apk add --no-cache git openssh-client
