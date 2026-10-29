# syntax=docker/dockerfile:1.28
ARG UBUNTU_BASE_DIGEST=sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
FROM --platform=$BUILDPLATFORM ubuntu:24.04@${UBUNTU_BASE_DIGEST}

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH
ARG RUNNER_VERSION=2.336.0
ARG RUNNER_SHA256_ARM64=58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1
ARG RUNNER_SHA256_X64=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d
ARG NODE_MAJOR=24

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    RUNNER_ROOT=/opt/actions-runner \
    RUNNER_VERSION=$RUNNER_VERSION \
    RUNNER_SHA256_ARM64=$RUNNER_SHA256_ARM64 \
    RUNNER_SHA256_X64=$RUNNER_SHA256_X64

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl file git gnupg gzip gosu jq lsb-release \
      shellcheck tini tar unzip wget xz-utils zip \
      python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

# Install Docker client plugins only. No dockerd, containerd or host socket.
RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      docker-ce-cli docker-compose-plugin docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

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
      --home-dir /home/runner --shell /bin/bash runner \
 && mkdir -p /home/runner /var/lib/general-runner/{state,runtime,work,cache} \
 && chown -R runner:runner /home/runner /var/lib/general-runner

COPY --chown=root:root scripts/fetch-runner.sh /usr/local/bin/fetch-runner
RUN chmod 0755 /usr/local/bin/fetch-runner \
 && TARGETARCH="$TARGETARCH" /usr/local/bin/fetch-runner \
 && /opt/actions-runner/bin/installdependencies.sh

COPY --chown=root:root scripts/register.sh /usr/local/bin/register
COPY --chown=root:root scripts/start-runner.sh /usr/local/bin/start-runner
COPY --chown=root:root scripts/probe.sh /usr/local/bin/probe
RUN chmod 0755 /usr/local/bin/register /usr/local/bin/start-runner /usr/local/bin/probe

LABEL org.opencontainers.image.title="general-runner" \
      org.opencontainers.image.description="Daemonless general-purpose GitHub Actions CI runner" \
      org.opencontainers.image.vendor="PintjesB"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-runner"]
CMD []
