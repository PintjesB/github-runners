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

for bin in bash curl git jq gh docker shellcheck node npm python3; do
    command -v "$bin" >/dev/null || { echo "missing $bin" >&2; exit 1; }
done
docker compose version
docker buildx version
[ "$(node -p 'process.versions.node.split(".")[0]')" = 24 ]
python3 -c 'import sys; assert sys.version_info >= (3, 12)'
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
python3 -m venv "$work/venv"
"$work/venv/bin/python" -m pip --version
! command -v codex >/dev/null 2>&1
! command -v titan-evidence >/dev/null 2>&1
echo "general runner capabilities verified without a Docker daemon"
