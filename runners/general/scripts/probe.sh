#!/usr/bin/env bash
set -euo pipefail

: "${EXPECTED_ARCH:?EXPECTED_ARCH must be amd64 or arm64}"
case "$EXPECTED_ARCH" in amd64|arm64) ;; *) exit 1 ;; esac
case "$(uname -m)" in
    x86_64|amd64) actual_arch=amd64 ;;
    aarch64|arm64) actual_arch=arm64 ;;
    *) echo "unsupported architecture" >&2; exit 1 ;;
esac
[ "$actual_arch" = "$EXPECTED_ARCH" ] || { echo "architecture mismatch" >&2; exit 1; }

for bin in bash curl git jq gh docker shellcheck node npm python3 psql; do
    command -v "$bin" >/dev/null || { echo "missing $bin" >&2; exit 1; }
done
docker compose version
docker buildx version
# Ubuntu 24.04 provides PostgreSQL client 16 on both amd64 and arm64.
# A mismatched or missing psql fails the native image probe before publishing.
psql_version="$(psql --version)"
case "$psql_version" in
    *" 16."*) ;;
    *) echo "unexpected PostgreSQL client version: $psql_version" >&2; exit 1 ;;
esac
# Verify the actual installed binary, not just Dockerfile source or a tag.
# The 1.0.0 published image was still built with Runner 2.336.0 even after
# the source was upgraded. Reject a stale or mismatched binary at CI build time.
: "${RUNNER_VERSION:?RUNNER_VERSION must be set by the image}"
runner_bin="${RUNNER_ROOT:-/opt/actions-runner}/bin/Runner.Listener"
[ -x "$runner_bin" ] || { echo "missing runner listener: $runner_bin" >&2; exit 1; }
installed_version="$("$runner_bin" --version)"
[ "$installed_version" = "$RUNNER_VERSION" ] || {
    printf 'installed runner %s differs from image RUNNER_VERSION %s\n' "$installed_version" "$RUNNER_VERSION" >&2
    exit 1
}
minimum_version=2.338.2
[ "$(printf '%s\n' "$minimum_version" "$installed_version" | sort -V | head -n 1)" = "$minimum_version" ] || {
    printf 'refusing deprecated GitHub runner %s (minimum %s)\n' "$installed_version" "$minimum_version" >&2
    exit 1
}
[ "$(node -p 'process.versions.node.split(".")[0]')" = 24 ]
python3 -c 'import sys; assert sys.version_info >= (3, 12)'
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
python3 -m venv "$work/venv"
"$work/venv/bin/python" -m pip --version
! command -v codex >/dev/null 2>&1
! command -v titan-evidence >/dev/null 2>&1
echo "general runner capabilities verified without a Docker daemon"
