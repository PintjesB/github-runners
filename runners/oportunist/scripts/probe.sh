#!/usr/bin/env bash
set -euo pipefail

: "${EXPECTED_ARCH:?EXPECTED_ARCH is required (amd64 or arm64)}"

ok() { printf '  ok   %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*" >&2; exit 1; }

titan-evidence --help >/dev/null

for binary in docker gh git bash node npm python3 codex lsb_release; do
    command -v "$binary" >/dev/null 2>&1 || fail "missing binary: $binary"
    ok "binary: $binary"
done

docker compose version >/dev/null 2>&1 || fail "docker compose unavailable"
docker buildx version >/dev/null 2>&1 || fail "docker buildx unavailable"
lsb_release -ds >/dev/null 2>&1 || fail "lsb_release unavailable"

info="$(docker info --format '{{.Architecture}}' 2>/dev/null)" || fail "docker daemon unreachable"
case "$info" in
    amd64|x86_64) daemon_arch=amd64 ;;
    arm64|aarch64) daemon_arch=arm64 ;;
    *) fail "unsupported docker daemon architecture: $info" ;;
esac
[ "$daemon_arch" = "$EXPECTED_ARCH" ] || fail "docker daemon architecture $daemon_arch != $EXPECTED_ARCH"
ok "docker daemon: $daemon_arch"

python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit(f"Python 3.12+ required, got {sys.version.split()[0]}")
PY
ok "python: $(python3 --version)"

node_major="$(node -p 'process.versions.node.split(".")[0]')"
[ "$node_major" = 24 ] || fail "Node 24 required, got $(node --version)"
ok "node: $(node --version)"

codex_version="$(codex --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
[ "$codex_version" = "${CODEX_VERSION:-0.147.0}" ] || fail "Codex version mismatch: $codex_version"
ok "codex: $codex_version"

getent hosts host.docker.internal >/dev/null 2>&1 || fail "host.docker.internal does not resolve"
ok "host gateway alias resolves"

if [ "${1:-}" != "--skip-network" ]; then
    curl -fsS --max-time 8 https://api.github.com/ >/dev/null || fail "GitHub API unreachable"
    ok "GitHub API reachable"
fi

printf 'all Oportunist runner capabilities verified\n'
