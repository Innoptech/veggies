# veggies canvas image: OpenHands Agent Canvas + a podman-remote client so
# the control plane can spawn ACP sessions in the harness container
# (ADR 0025). Base pinned by tag AND digest; bump both together.
FROM ghcr.io/openhands/agent-canvas:1.16.0@sha256:ab194760cb46098641747b27c1e07458ab1bed439821af7a31c97133ca466bb8

# Build-time proxy args: on the VPS the stacks user is direct-egress-denied,
# so image builds ride the filtering proxy (same pattern as
# opencode.Containerfile). python3 does the download (the base has no curl).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG NO_PROXY=""

USER root
# podman-remote-static v5.8.4 - keep in lockstep with the host podman
# (VPS runs 5.8.x; a v6 client refuses a v5 server). Pinned by sha256.
RUN python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/containers/podman/releases/download/v5.8.4/podman-remote-static-linux_amd64.tar.gz', '/tmp/pr.tgz')" \
 && echo "3d2a1a45f668fe5c240deb8df83d9e2e415429fb91f8a7442985a636edf33654  /tmp/pr.tgz" | sha256sum -c - \
 && tar xzf /tmp/pr.tgz -C /usr/local/bin --strip-components=1 bin/podman-remote-static-linux_amd64 \
 && mv /usr/local/bin/podman-remote-static-linux_amd64 /usr/local/bin/podman \
 && chmod 0755 /usr/local/bin/podman && rm /tmp/pr.tgz \
# the container runs as container-root (-> the stack user on the host);
# make HOME writable regardless of uid games.
 && chmod -R a+rwX /home/openhands

USER openhands
