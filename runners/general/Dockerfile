# syntax=docker/dockerfile:1.28
ARG UBUNTU_BASE_DIGEST=sha256:2198e4c65076e9cb0b3d695219df667c0e3b0072da64ca3c2648aca17dcecaf5
FROM --platform=$BUILDPLATFORM ubuntu:24.04@${UBUNTU_BASE_DIGEST}

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH
ARG RUNNER_VERSION=2.338.2
ARG RUNNER_SHA256_ARM64=670adb2f268fe21dcbb58b3db0e0aa9d5b3bddfe2c651481336e561ff86de945
ARG RUNNER_SHA256_X64=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
ARG NODE_MAJOR=24

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    RUNNER_ROOT=/opt/actions-runner \
    RUNNER_VERSION=$RUNNER_VERSION \
    RUNNER_SHA256_ARM64=$RUNNER_SHA256_ARM64 \
    RUNNER_SHA256_X64=$RUNNER_SHA256_X64

# Keep the Ubuntu signing key and package suites when the default mirrors are unreachable.
RUN set -e; \
    if ! apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=8 -o APT::Update::Error-Mode=any update; then \
      sed -i \
        -e 's|://archive\.ubuntu\.com/ubuntu|://mirrors.mit.edu/ubuntu|g' \
        -e 's|://security\.ubuntu\.com/ubuntu|://mirrors.mit.edu/ubuntu|g' \
        -e 's|://ports\.ubuntu\.com/ubuntu-ports|://mirrors.mit.edu/ubuntu-ports|g' \
        /etc/apt/sources.list.d/ubuntu.sources; \
      apt-get -o APT::Update::Error-Mode=any update; \
    fi; \
    apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl file git gnupg gzip gosu jq lsb-release \
      shellcheck tini tar unzip wget xz-utils zip \
      python3 python3-pip python3-venv postgresql-client \
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
