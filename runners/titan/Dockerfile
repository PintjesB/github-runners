# syntax=docker/dockerfile:1.7
#
# Titan Stocks self-hosted GitHub Actions runner image.
#
# This image powers the persistent self-hosted runner that executes
# every job from the private Titan Stocks repository. The image is
# published as a multi-platform manifest supporting both
# ``linux/amd64`` and ``linux/arm64``; an application job runs once
# on whichever compatible ``titan-ci`` runner is available. The
# dedicated VM remains the security boundary: the runner mounts the
# VM's Docker socket directly and reaches the daemon as a
# supplemental group on the runner user.
#
# Registration consumes a single short-lived registration token
# through an allowlisted ``RUNNER_TOKEN`` env var; the long-running
# listener authenticates with the GitHub-issued secret persisted in
# a dedicated state volume, attaches to a normal Compose bridge
# network, and listens for jobs until it is redeployed.
#
# Build:
#
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     --tag ghcr.io/pintjesb/titan-stocks-runner:dev --load .
#
# BuildKit supplies an automatic ``TARGETARCH`` build argument.
# ``fetch-runner.sh`` maps it to the upstream archive naming
# (amd64 -> x64, arm64 -> arm64) and verifies the architecture-
# specific pinned digest before any download attempt. Every other
# architecture is refused up front so a wrong platform never reaches
# the network.
#
# Update procedure:
#
#   1. ``RUNNER_VERSION`` below must track the current GitHub-
#      supported ``v2.x`` line. GitHub ends runner support 30 days
#      after a new release; this image MUST be rebuilt before the
#      cutoff.
#   2. ``RUNNER_SHA256_ARM64`` is the SHA-256 of the upstream
#      tarball ``actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz``
#      and ``RUNNER_SHA256_X64`` is the SHA-256 of the upstream
#      tarball ``actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz``;
#      both digests are published by ``actions/runner``. Refresh
#      all three values together; the lifecycle scripts verify the
#      architecture-specific digest before any job is allowed to
#      register against GitHub.
#   3. The capability probe's Playwright version is owned by
#      ``probe/package.json`` and its lockfile so Renovate updates
#      one canonical dependency source.
#   4. ``CODEX_VERSION`` pins the Codex CLI used by autonomous
#      workflows. Refresh it deliberately so image publication and
#      the native capability probe validate the exact CLI version.
#
# The image runs as a non-root ``runner`` user. The Docker socket
# is bind-mounted read/write by the persistent Compose configuration
# and the host socket's group ID is mapped onto the runner user at
# start time so the docker CLI can reach the daemon without sudo.

ARG UBUNTU_BASE_DIGEST=sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea
FROM --platform=$BUILDPLATFORM ubuntu:24.04@${UBUNTU_BASE_DIGEST} AS base

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH
ARG RUNNER_VERSION=2.336.0
ARG RUNNER_SHA256_ARM64=58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1
ARG RUNNER_SHA256_X64=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d
ARG CODEX_VERSION=0.147.0

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    RUNNER_VERSION=${RUNNER_VERSION} \
    RUNNER_SHA256_ARM64=${RUNNER_SHA256_ARM64} \
    RUNNER_SHA256_X64=${RUNNER_SHA256_X64} \
    CODEX_VERSION=${CODEX_VERSION} \
    CODEX_HOME=/home/runner/.codex \
    # The image installs Node via NodeSource; declaring the major
    # here keeps the Docker layer cache stable across rebuilds that
    # only refresh the runner or probe dependencies.
    NODE_MAJOR=24 \
    # The runner binaries live in this image-owned directory. The
    # container entrypoint does not need to redownload them.
    RUNNER_ROOT=/opt/actions-runner

# Refresh the base image's package index, install the documented
# CI toolchain, and create the non-root ``runner`` user. The
# system packages mirror the canonical ``actions/runner``
# container plus the Playwright Chromium runtime dependency set.
# ``tini`` provides PID 1 for the ``init: true`` Compose contract
# so the runner child processes get proper signal forwarding and
# zombie reaping.
#
# ``ca-certificates`` ships the Mozilla CA bundle so GitHub API
# calls and Playwright browser downloads all use a trusted store.
# ``gnupg`` is required for the NodeSource apt keyring pattern;
# ``jq`` powers the runner registration token exchange. ``gosu``
# is required for the persistent listener to drop from the root
# entrypoint to the unprivileged ``runner`` user.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        git \
        bash \
        coreutils \
        tar \
        gzip \
        xz-utils \
        unzip \
        zip \
        file \
        gnupg \
        jq \
        tini \
        gosu \
        # Docker CLI + Compose v2 plugin + Buildx (CLI plugins ship
        # under /usr/local/lib/docker/cli-plugins so the docker CLI
        # discovers them automatically).
        docker.io \
        docker-compose-v2 \
        docker-buildx \
        # PostgreSQL client tooling used by the capability probe
        # and by downstream test stacks.
        postgresql-client \
        # ShellCheck for the workflow ``run_gate`` and visual
        # Playwright jobs.
        shellcheck \
        # Playwright Chromium system dependencies. The list matches
        # the Ubuntu 24.04 requirements documented by the Playwright
        # project; we install them at image build time so workflow
        # jobs never need ``--with-deps`` and never need sudo.
        libasound2t64 \
        libatk-bridge2.0-0t64 \
        libatk1.0-0t64 \
        libcairo2 \
        libcups2t64 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0t64 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
 && rm -rf /var/lib/apt/lists/*

# Install Node.js 24.x via NodeSource. The ``setup-node`` action in
# the workflow reuses the same ``.nvmrc`` (24.18.0); the system
# install is the fallback that keeps the runner functional even if
# the GitHub-hosted action ever fails to fetch.
RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key" \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && node --version \
 && npm --version

# Install a pinned Codex CLI globally. Authentication is intentionally
# NOT baked into the image; Compose mounts a dedicated named volume at
# ``CODEX_HOME`` and operators perform the ChatGPT device login once
# after deployment. The same image therefore remains safe to publish
# publicly while the replaceable login state stays local to the CI VM.
RUN npm install -g --no-audit --no-fund "@openai/codex@${CODEX_VERSION}" \
 && codex --version \
 && npm cache clean --force

# Install the GitHub CLI from the official apt repository. The CI
# host uses ``gh`` for the runner-smoke capability probe and for
# occasional diagnostic invocations against the API.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && gh --version

# Install Python 3.12 from the deadsnakes PPA. The runner needs
# the interpreter for the ``setup-python`` action fallback and
# for the capability probe; application Python dependencies are
# still installed by the workflow from its own hash-locked
# requirements files.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv python3.12-dev \
 && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
 && python3 --version \
 && rm -rf /var/lib/apt/lists/*

# Create the non-root ``runner`` user. ``RUNNER_UID`` matches the
# deployment's conventional 1001. The start-up script maps the
# bind-mounted Docker socket's group onto the runner user as a
# *supplemental* group; the runner user's *primary* group remains
# ``runner``.
#
# The home directory hosts the canonical Actions runner checkout,
# the ``_work`` job workspace, the Codex login directory, and the
# Playwright ``.cache`` for browser binaries. Persistent volumes
# overlay the mutable directories in production.
RUN groupadd --system --gid 1001 runner \
 && useradd --system --uid 1001 --gid 1001 \
        --home-dir /home/runner \
        --shell /bin/bash \
        --comment "Titan Stocks CI runner" \
        runner \
 && mkdir -p /home/runner/.cache/ms-playwright \
             /home/runner/.codex \
             /home/runner/actions-runner \
             /home/runner/.local/bin \
             /var/lib/titan-runner/{state,work,browser} \
 && chown -R runner:runner /home/runner /var/lib/titan-runner \
 && chmod 0755 /home/runner \
 && chmod 0755 /var/lib/titan-runner/{state,work,browser}

# Pin the Actions runner tarball by SHA-256. ``fetch-runner.sh``
# maps the automatic ``TARGETARCH`` build argument to the
# upstream archive name (amd64 -> x64, arm64 -> arm64), selects
# the architecture-specific digest, and refuses every other
# architecture before any download attempt. The tarball is
# extracted into ``RUNNER_ROOT`` (default ``/opt/actions-runner``);
# the entrypoint symlinks that directory into ``$HOME/actions-runner``
# on first start so the upstream ``run.sh`` finds its sibling
# files.
COPY --chown=root:root scripts/titan-evidence /usr/local/bin/titan-evidence
RUN chmod 0755 /usr/local/bin/titan-evidence

COPY --chown=root:root scripts/fetch-runner.sh /usr/local/bin/fetch-runner
RUN chmod 0755 /usr/local/bin/fetch-runner \
 && TARGETARCH="${TARGETARCH}" /usr/local/bin/fetch-runner

# Install a deterministic ``playwright-core`` dependency tree that
# the capability probe consumes at runtime. A committed
# ``package-lock.json`` pins the resolved version so the image is
# reproducible without ever invoking ``npx`` at probe time.
# ``npx`` interprets its first positional argument as a binary to
# execute, not a package to install, so it cannot be used to
# "install playwright-core and then run node".
COPY probe/package.json /opt/titan-probe/package.json
COPY probe/package-lock.json /opt/titan-probe/package-lock.json
RUN cd /opt/titan-probe && npm ci --omit=dev --no-audit --no-fund \
 && npm cache clean --force

# Install Playwright system browsers as the runner user. The
# Chromium binary is reused for both Playwright application runs
# and the ``probe`` capability check. We install only the Chromium
# binary (the OS libraries were installed earlier in this
# Dockerfile) into the baked image cache, which ``start-runner``
# seeds into the persistent browser volume on first start.
USER runner
WORKDIR /home/runner
ENV PLAYWRIGHT_BROWSERS_PATH=/home/runner/.cache/ms-playwright
RUN /opt/titan-probe/node_modules/.bin/playwright-core install chromium \
 && ls -1 /home/runner/.cache/ms-playwright

USER root

# Stage the runner lifecycle scripts. ``register`` reads the
# short-lived registration token from the allowlisted
# ``RUNNER_TOKEN`` env var and persists the resulting ``.runner``
# and ``.credentials*`` artifacts in the state volume.
# ``start-runner`` invokes the upstream ``run.sh`` listener as a
# persistent runner. ``probe`` exposes every documented capability
# for the smoke workflow.
COPY scripts/register.sh /usr/local/bin/register
COPY scripts/start-runner.sh /usr/local/bin/start-runner
COPY scripts/probe.sh /usr/local/bin/probe
COPY scripts/upstream-version.sh /usr/local/bin/upstream-version
RUN chmod 0755 /usr/local/bin/register \
                /usr/local/bin/start-runner \
                /usr/local/bin/probe \
                /usr/local/bin/upstream-version

# Stage the bounded GitHub Actions pre/post-job hooks.
# ``pre-job.sh`` validates that required host capabilities and the
# ``host.docker.internal`` alias are available before the job
# starts. ``post-job.sh`` removes only Titan-prefixed temporary
# Compose projects and their project-owned volumes; it never
# touches the external runner volumes, invokes ``docker system prune``,
# or recurses the persistent ``_work`` directory. The hooks live inside
# the image-owned runner tree so
# every materialised runtime tree carries them.
RUN install -d -m 0755 /opt/actions-runner/.hooks
COPY scripts/pre-job.sh /opt/actions-runner/.hooks/PreJob.sh
COPY scripts/post-job.sh /opt/actions-runner/.hooks/PostJob.sh
RUN chmod 0755 /opt/actions-runner/.hooks/PreJob.sh \
                /opt/actions-runner/.hooks/PostJob.sh

# Document the expected labels so operators can spot misconfigured
# deployments at a glance. The labels are passed at registration
# time so an image rebuild is not required to retarget a temporary
# host.
#
# The image is intentionally published without an OCI license
# label. The repository's README states that publication does not
# grant any reuse rights and the project does not ship a
# ``LICENSE`` file.
LABEL org.opencontainers.image.title="titan-stocks-runner" \
      org.opencontainers.image.description="Persistent multi-platform (linux/amd64, linux/arm64) GitHub Actions runner for Titan Stocks (capability-complete, no auto-update)" \
      org.opencontainers.image.source="https://github.com/PintjesB/titan-stocks-runner" \
      org.opencontainers.image.vendor="PintjesB"

# The image-owned ``tini`` entrypoint runs ``start-runner``. That
# startup script performs the idempotent registration phase in this
# same container, unsets the short-lived token, and then launches the
# persistent listener.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-runner"]
CMD []
