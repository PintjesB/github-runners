#!/usr/bin/env bash
set -euo pipefail

while IFS= read -r script; do
    bash -n "$script"
    shellcheck -S warning "$script"
done < <(find scripts -type f -name '*.sh' -print)

docker compose --env-file .env.example config >/dev/null

grep -q '@openai/codex' Dockerfile
grep -q 'docker-compose-v2' Dockerfile
grep -q 'docker-buildx' Dockerfile
grep -q 'python3' Dockerfile
grep -q 'nodejs' Dockerfile
grep -q 'lsb-release' Dockerfile
! grep -qi 'playwright' Dockerfile
! grep -qi 'chromium' Dockerfile
! grep -qi 'postgresql-client' Dockerfile

grep -q 'oportunist-runner-state' docker-compose.yml
grep -q 'oportunist-runner-work' docker-compose.yml
grep -q 'oportunist-runner-codex' docker-compose.yml
grep -q 'CODEX_HOME: /home/runner/.codex' docker-compose.yml

grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' VERSION

repo_root="$(cd ../.. && pwd)"
wrapper="$repo_root/.github/workflows/publish-oportunist.yml"
semver="$repo_root/.github/workflows/_tag-semver.yml"

grep -q 'uses: ./\.github/workflows/_tag-semver.yml' "$wrapper"
grep -q 'profile: oportunist' "$wrapper"
grep -q 'image: ghcr.io/pintjesb/oportunist-runner' "$wrapper"
grep -q 'needs: publish' "$wrapper"
grep -q 'version_file="runners/${PROFILE}/VERSION"' "$semver"
grep -q 'gh api --paginate' "$semver"
grep -q 'version collision:' "$semver"

python3 -m py_compile scripts/titan-evidence
scripts/titan-evidence --help >/dev/null
cmp scripts/titan-evidence ../titan/scripts/titan-evidence
