# Minimal squid image: distro package, no third-party image trust.
FROM docker.io/library/ubuntu:24.04

# Build-time proxy args: on the VPS the stacks user is direct-egress-denied,
# so image builds must ride the filtering proxy. buildah exposes ARGs to RUN
# steps as env (both cases: apk/pip read lowercase, curl reads either).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG NO_PROXY=""

RUN apt-get update \
    && apt-get install -y --no-install-recommends squid \
    && rm -rf /var/lib/apt/lists/*

# Config and allowlist are bind-mounted read-only by the quadlet.
ENTRYPOINT ["/usr/sbin/squid", "-N", "--foreground"]
