# veggies opencode image: THIS REPO's harness variant (ADR 0053) - the
# pinned shared base (harness only: opencode serve, git, gh) plus this
# repo's ADR 0032 dogfood toolchain (python/mask/ansible/tofu/tflint, plus
# gitleaks/actionlint for the networkless pre-commit hooks, ADR 0047). The
# runtime rootfs is read-only, so nothing can be installed later. The agent
# runs the repo's own checks in-container (`mask ci`, minus molecule: the
# podman socket stays banned, ADR 0028).
# The base is built first by `ensure_images` from
# deploy/images/opencode-base.Containerfile - this file is NOT
# standalone-buildable (a bare `podman build` would try to pull from a
# registry literally named localhost). The overlay pins the base by tag;
# the base pins the upstream image by tag+digest. Bump all four spots
# together (both Containerfiles, IMAGE_OPENCODE_BASE/IMAGE_OPENCODE in
# cli/components/opencode.py) - tests/test_veggies.py enforces the lockstep.
FROM localhost/veggies-opencode-base:1.18.27

# Build-time proxy args (same contract as opencode-base.Containerfile).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG NO_PROXY=""

# bash: mask targets and scripts/check_vault_encrypted.sh are bash, not
# busybox sh. unzip/curl: release fetch below.
RUN apk add --no-cache bash curl unzip python3 py3-pip

# Python side pinned to requirements-dev.txt (molecule excluded on
# purpose). Alpine's python is externally-managed; this is an image build,
# not a runtime pip install, hence --break-system-packages.
RUN pip install --break-system-packages --no-cache-dir \
    ansible-core==2.21.3 ansible-lint==26.8.0 pre-commit==4.6.2 \
    yamllint==1.38.0 pytest==9.1.1

# Static binaries, version + sha256 pinned (same supply-chain discipline
# as the upstream base image digest). mask ships a musl build for alpine.
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

# In-pod egress rides the chained squid; cold fetches measured 20-35s vs
# tofu's 10s default registry timeout, and init always fetches the registry
# discovery doc (issue #50). ENV so every in-pod tofu run - any dogfooded
# repo, not just mask ci here - tolerates cold egress.
ENV TF_REGISTRY_CLIENT_TIMEOUT=120

# gitleaks + actionlint: the pre-commit hooks run these via language: system
# (issue #48, ADR 0047) - hook time involves zero network fetches. Same
# version+sha256 pin discipline as above.
ARG GITLEAK_VERSION=8.30.1
ARG GITLEAK_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
ARG ACTIONLINT_VERSION=1.7.12
ARG ACTIONLINT_SHA256=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
RUN set -eux; cd /tmp; \
    curl -fsSL -o gitleaks.tar.gz "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAK_VERSION}/gitleaks_${GITLEAK_VERSION}_linux_x64.tar.gz"; \
    echo "${GITLEAK_SHA256}  gitleaks.tar.gz" | sha256sum -c -; \
    tar xzf gitleaks.tar.gz gitleaks; \
    mv gitleaks /usr/local/bin/gitleaks; \
    curl -fsSL -o actionlint.tar.gz "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"; \
    echo "${ACTIONLINT_SHA256}  actionlint.tar.gz" | sha256sum -c -; \
    tar xzf actionlint.tar.gz actionlint; \
    mv actionlint /usr/local/bin/actionlint; \
    chmod +x /usr/local/bin/gitleaks /usr/local/bin/actionlint; \
    rm -f /tmp/gitleaks.tar.gz /tmp/actionlint.tar.gz
