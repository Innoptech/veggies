# Minimal squid image: distro package, no third-party image trust.
FROM docker.io/library/ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends squid \
    && rm -rf /var/lib/apt/lists/*

# Config and allowlist are bind-mounted read-only by the quadlet.
ENTRYPOINT ["/usr/sbin/squid", "-N", "--foreground"]
