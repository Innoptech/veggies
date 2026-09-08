FROM docker.io/library/python:3.13-alpine

# Build-time proxy args: on the VPS the stacks user is direct-egress-denied,
# so image builds must ride the filtering proxy. buildah exposes ARGs to RUN
# steps as env (both cases: apk/pip read lowercase, curl reads either).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG NO_PROXY=""

# pyyaml + jinja2 are the only deps beyond stdlib (core.py needs both).
RUN pip install --no-cache-dir pyyaml==6.0.2 jinja2==3.1.6

# The code ships via the stack-config mount (config_files), not the image:
# restart picks up changes without a rebuild.
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "/stack-config/orchestrator-server.py"]
