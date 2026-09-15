# veggies github-auth sidecar image (ADR 0063): python + the pinned JWT
# stack the App-token minter needs. The daemon and the minter ship as
# stack-config files, so this image is pure runtime. Base pinned by tag AND
# digest (same base as the MCP toolbox image); bump both together. The
# PyJWT pin tracks requirements-dev.txt and the github_runner role's dnf
# python3-jwt - one minter, one library version everywhere it runs.
FROM docker.io/library/python:3.13-alpine@sha256:46ee549c88617e9bc8acb843a326f1a5c0fa5608d7f9703509efe6d53b55f318

# Build-time proxy args: on the VPS the stacks user is direct-egress-denied,
# so image builds must ride the filtering proxy. buildah exposes ARGs to RUN
# steps as env (both cases: pip reads lowercase).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG NO_PROXY=""

RUN pip install --no-cache-dir PyJWT==2.10.1 cryptography==46.0.3
