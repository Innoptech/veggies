# opencode server image (ADR 0012). Ubuntu userland; opencode tarball pinned
# by version (no upstream checksums - see the role defaults for the note).
FROM docker.io/library/ubuntu:24.04

ARG OPENCODE_VERSION

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git tmux unzip \
    && rm -rf /var/lib/apt/lists/*

# TODO(verify): tarball layout (expects a bare `opencode` binary at top level).
RUN curl -sfL -o /tmp/opencode.tgz \
      "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
    && tar -xzf /tmp/opencode.tgz -C /usr/local/bin \
    && rm /tmp/opencode.tgz \
    && opencode --version
