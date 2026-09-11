# veggies opencode image: official image + git, which the official
# image lacks (verified 2026-09-04: alpine-based, root, no git/node),
# plus gh for github-enabled stacks (ADR 0030): gh reads GH_TOKEN and
# api.github.com is already on the squid allowlist.
# ADR 0032: the dogfooding toolchain (python/mask/ansible/tofu/tflint)
# lives in the image - the runtime rootfs is read-only, so nothing can be
# installed later. The agent runs the repo's own checks in-container
# (`mask ci`, minus molecule: the podman socket stays banned, ADR 0028).
# Base pinned by tag AND digest; bump both together.
FROM ghcr.io/anomalyco/opencode:1.18.27@sha256:1eedcb5d4439130e35f5cf76d87c786c4eeb12dc7afebd79663f6c8341fa8505

# Build-time proxy args: on the VPS the stacks user is direct-egress-denied,
# so image builds must ride the filtering proxy. buildah exposes ARGs to RUN
# steps as env (both cases: apk/pip read lowercase, curl reads either).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG NO_PROXY=""

# bash: mask targets and scripts/check_vault_encrypted.sh are bash, not
# busybox sh. unzip/curl: release fetch below.
RUN apk add --no-cache git openssh-client github-cli bash curl unzip python3 py3-pip

# Python side pinned to requirements-dev.txt (molecule excluded on
# purpose). Alpine's python is externally-managed; this is an image build,
# not a runtime pip install, hence --break-system-packages.
RUN pip install --break-system-packages --no-cache-dir \
    ansible-core==2.21.3 ansible-lint==26.8.0 pre-commit==4.6.2 \
    yamllint==1.38.0 pytest==9.1.1

# Static binaries, version + sha256 pinned (same supply-chain discipline
# as the base image digest). mask ships a musl build for alpine.
ARG MASK_VERSION=0.11.7
ARG MASK_SHA256=5c9fed48ecd6a9cbbf7332d67258930c0b2fcc18689850ce617b899c0eeae0c9
ARG TOFU_VERSION=1.12.6
ARG TOFU_SHA256=5dc43da4f750f33873dc25e94587128709e819e544b7be9016b255316153c3a8
ARG TFLINT_VERSION=0.64.0
ARG TFLINT_SHA256=cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537
RUN set -eux; cd /tmp; \
    curl -fsSL -o mask.zip "https://github.com/jacobdeichert/mask/releases/download/mask/${MASK_VERSION}/mask-${MASK_VERSION}-x86_64-unknown-linux-musl.zip"; \
    echo "${MASK_SHA256}  mask.zip" | sha256sum -c -; \
    unzip -q mask.zip -d mask-x; \
    mv "mask-x/mask-${MASK_VERSION}-x86_64-unknown-linux-musl/mask" /usr/local/bin/mask; \
    curl -fsSL -o tofu.zip "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip"; \
    echo "${TOFU_SHA256}  tofu.zip" | sha256sum -c -; \
    unzip -q tofu.zip tofu -d /usr/local/bin; \
    curl -fsSL -o tflint.zip "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/tflint_linux_amd64.zip"; \
    echo "${TFLINT_SHA256}  tflint.zip" | sha256sum -c -; \
    unzip -q tflint.zip tflint -d /usr/local/bin; \
    chmod +x /usr/local/bin/mask /usr/local/bin/tofu /usr/local/bin/tflint; \
    rm -rf /tmp/mask.zip /tmp/mask-x /tmp/tofu.zip /tmp/tflint.zip
