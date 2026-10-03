# syntax=docker/dockerfile:1.27

ARG UBUNTU_BASE_DIGEST=sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea
FROM --platform=$BUILDPLATFORM ubuntu:24.04@${UBUNTU_BASE_DIGEST}

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH
ARG RUNNER_VERSION=2.336.0
ARG RUNNER_SHA256_ARM64=58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1
ARG RUNNER_SHA256_X64=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d
ARG CODEX_VERSION=0.147.0

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NODE_MAJOR=24 \
    RUNNER_ROOT=/opt/actions-runner \
    RUNNER_VERSION=${RUNNER_VERSION} \
    RUNNER_SHA256_ARM64=${RUNNER_SHA256_ARM64} \
    RUNNER_SHA256_X64=${RUNNER_SHA256_X64} \
    CODEX_VERSION=${CODEX_VERSION} \
    CODEX_HOME=/home/runner/.codex

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl file git gnupg gzip gosu jq tini tar unzip wget xz-utils zip \
      docker.io docker-compose-v2 docker-buildx \
      python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && node --version \
 && npm --version

RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 runner \
 && useradd --system --uid 1001 --gid 1001 \
      --home-dir /home/runner --shell /bin/bash \
      --comment "Oportunist GitHub Actions runner" runner \
 && mkdir -p /home/runner/.codex /var/lib/oportunist-runner/{state,runtime,work} \
 && chown -R runner:runner /home/runner /var/lib/oportunist-runner

RUN npm install -g "@openai/codex@${CODEX_VERSION}" \
 && codex --version

COPY --chown=root:root scripts/titan-evidence /usr/local/bin/titan-evidence
RUN chmod 0755 /usr/local/bin/titan-evidence

COPY --chown=root:root scripts/fetch-runner.sh /usr/local/bin/fetch-runner
RUN chmod 0755 /usr/local/bin/fetch-runner \
 && TARGETARCH="${TARGETARCH}" /usr/local/bin/fetch-runner \
 && /opt/actions-runner/bin/installdependencies.sh

COPY --chown=root:root scripts/register.sh /usr/local/bin/register
COPY --chown=root:root scripts/start-runner.sh /usr/local/bin/start-runner
COPY --chown=root:root scripts/probe.sh /usr/local/bin/probe
RUN chmod 0755 /usr/local/bin/register /usr/local/bin/start-runner /usr/local/bin/probe

LABEL org.opencontainers.image.title="oportunist-runner" \
      org.opencontainers.image.description="Lean persistent GitHub Actions and Codex runner for Oportunist" \
      org.opencontainers.image.vendor="PintjesB"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-runner"]
CMD []
