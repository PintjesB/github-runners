#!/usr/bin/env bash
set -euo pipefail
for script in scripts/*.sh; do
    bash -n "$script"
    shellcheck -S warning "$script"
done
docker compose --env-file .env.example config --quiet
grep -q '^FROM --platform=' Dockerfile
grep -q 'docker-ce-cli docker-compose-plugin docker-buildx-plugin' Dockerfile
grep -q 'python3-pip python3-venv' Dockerfile
! grep -Eq '(@openai/codex|CODEX_VERSION|postgresql-client|playwright)' Dockerfile
! grep -q '/var/run/docker.sock' docker-compose.yml
grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' VERSION
