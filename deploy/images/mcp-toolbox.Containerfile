# veggies MCP toolbox image: python + pinned fastmcp. The server itself
# ships as a stack-config file (ADR 0018; same pattern as the canvas critic
# shim), so this image is pure runtime. Base pinned by tag AND digest;
# bump both together.
FROM docker.io/library/python:3.13-alpine@sha256:46ee549c88617e9bc8acb843a326f1a5c0fa5608d7f9703509efe6d53b55f318

# Build-time proxy args: on the VPS the stacks user is direct-egress-denied,
# so image builds must ride the filtering proxy. buildah exposes ARGs to RUN
# steps as env (both cases: pip reads lowercase, curl reads either).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG NO_PROXY=""

RUN pip install --no-cache-dir fastmcp==4.0.3
