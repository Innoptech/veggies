# Ephemeral GitHub Actions runner on Ubuntu 24.04 (ADR 0005/0009).
# Ubuntu userland on purpose: workflow parity with GitHub-hosted runners.
FROM docker.io/library/ubuntu:24.04

ARG RUNNER_VERSION
ARG RUNNER_SHA256

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git jq tar gzip unzip sudo docker.io \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1001 runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/runner

WORKDIR /home/runner

RUN curl -sfL -o runner.tgz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && echo "${RUNNER_SHA256}  runner.tgz" | sha256sum -c - \
    && tar xzf runner.tgz \
    && rm runner.tgz \
    && ./bin/installdependencies.sh

COPY runner-entrypoint.sh /usr/local/bin/runner-entrypoint
RUN chmod 0755 /usr/local/bin/runner-entrypoint && chown -R runner:runner /home/runner

USER runner
ENTRYPOINT ["/usr/local/bin/runner-entrypoint"]
