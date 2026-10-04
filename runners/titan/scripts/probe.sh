#!/usr/bin/env bash
# Capability probe for the Titan Stocks self-hosted runner image.
#
# This script exercises the documented capability contract without
# registering against GitHub. Operators invoke it directly through
# ``deploy.sh probe`` to confirm a fresh image resolves every tool
# the workflows expect, or through the publish workflow to gate an
# image release on capability verification.
#
# ``deploy.sh`` passes the native host architecture (``amd64`` or
# ``arm64``) through the ``EXPECTED_ARCH`` env var so the probe can
# confirm the Docker daemon reaches the same native architecture
# the runner image was built for. A mismatch or an emulated
# architecture is rejected before the rest of the probe runs so a
# degraded host cannot pass the capability gate.
#
# Probes (in order):
#
#   1. Required binaries: docker, docker compose, buildx, the
#      ShellCheck binary, gh, git, bash, node, npm, codex, python3,
#      psql.
#   2. Docker daemon reachability through the bind-mounted socket.
#      The daemon's reported architecture MUST match ``EXPECTED_ARCH``
#      (``amd64`` or ``arm64``). Any other architecture or any
#      failure to parse ``EXPECTED_ARCH`` aborts the probe.
#   3. Compose v2 plugin resolution (parses ``docker compose version``).
#   4. Buildx availability (``docker buildx version``).
#   5. Node.js 24 major version and npm presence.
#   6. Pinned Codex CLI presence and version.
#   7. Python 3.12+ interpreter.
#   8. Playwright Chromium binary reachable at the documented
#      ``PLAYWRIGHT_BROWSERS_PATH`` and able to launch a headless
#      page through the deterministic ``/opt/titan-probe`` install.
#   9. Network reachability to the GitHub API (unless ``--skip-network``).
#  10. Host-gateway alias resolution. ``host.docker.internal`` must
#      resolve to an IPv4/IPv6 address; the listener reaches
#      workflow-published services through this alias.
#
# The probe never persists registration tokens and never touches
# the host filesystem outside the working directory.
set -euo pipefail

: "${EXPECTED_ARCH:?EXPECTED_ARCH is required (amd64 or arm64; deploy.sh maps uname -m to it)}"

case "$EXPECTED_ARCH" in
    amd64|arm64) ;;
    *)
        printf '  FAIL unsupported EXPECTED_ARCH: %s (expected amd64 or arm64)\n' "$EXPECTED_ARCH" >&2
        exit 1
        ;;
esac

PROBE_DIR="${PROBE_DIR:-${HOME:-/tmp}/.cache/probe}"
PROBE_NODE_MODULES="${PROBE_NODE_MODULES:-/opt/titan-probe/node_modules}"
mkdir -p "$PROBE_DIR"

ok()   { printf '  ok   %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*" >&2; exit 1; }

require_binary() {
    local name="$1"
    local path
    if ! path="$(command -v "$name" 2>/dev/null)"; then
        fail "required binary missing: $name"
    fi
    ok "binary: $name -> $path"
}

probe_docker() {
    require_binary docker
    local info
    info="$(docker info --format '{{.ServerVersion}}|{{.OSType}}/{{.Architecture}}' 2>&1)" \
        || fail "docker daemon unreachable: $info"
    case "$info" in
        *"/amd64"|*"/x86_64") daemon_arch="amd64" ;;
        *"/arm64"|*"/aarch64") daemon_arch="arm64" ;;
        *) fail "docker daemon architecture is unsupported (got $info)" ;;
    esac
    if [ "$daemon_arch" != "$EXPECTED_ARCH" ]; then
        fail "docker daemon architecture ($daemon_arch) does not match native runner architecture ($EXPECTED_ARCH); emulated/mismatched daemons are rejected"
    fi
    ok "docker daemon: $info (matches native runner architecture $EXPECTED_ARCH)"
}

probe_compose() {
    local version
    version="$(docker compose version --short 2>&1)" \
        || fail "docker compose plugin missing"
    ok "docker compose: $version"
}

probe_buildx() {
    local version
    version="$(docker buildx version 2>&1)" \
        || fail "docker buildx missing"
    ok "buildx: $version"
}

probe_shellcheck() {
    require_binary shellcheck
    local version
    version="$(shellcheck --version | sed -n 's/^version: //p' | head -n1)"
    ok "shellcheck: $version"
}

probe_gh() {
    require_binary gh
    local version
    version="$(gh --version | head -n1)"
    ok "gh: $version"
}

probe_node() {
    require_binary node
    require_binary npm
    local major
    major="$(node -e 'console.log(process.versions.node.split(".")[0])')"
    [ "$major" -eq 24 ] || fail "node major version must be 24 (got $major)"
    ok "node: v$(node --version)"
}

probe_codex() {
    require_binary codex
    local version
    version="$(codex --version 2>&1)" || fail "codex CLI failed to report its version"
    if [ -n "${CODEX_VERSION:-}" ] && ! printf '%s\n' "$version" | grep -Fq "$CODEX_VERSION"; then
        fail "codex version does not match image pin (expected $CODEX_VERSION, got $version)"
    fi
    ok "codex: $version"
}

probe_python() {
    require_binary lsb_release
    lsb_release -ds >/dev/null
    local python=""
    for candidate in python python3 python3.12; do
        if command -v "$candidate" >/dev/null 2>&1; then
            python="$candidate"
            break
        fi
    done
    [ -n "$python" ] || fail "no python interpreter on PATH"
    local version
    version="$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    case "$version" in
        3.1[2-9]) ok "python: $python ($version)" ;;
        *) fail "python must be 3.12+ (got $version)" ;;
    esac
}

probe_postgres_client() {
    require_binary psql
    local version
    version="$(psql --version)"
    ok "psql: $version"
}

probe_playwright() {
    local cache="${PLAYWRIGHT_BROWSERS_PATH:-/home/runner/.cache/ms-playwright}"
    [ -d "$cache" ] || fail "playwright cache missing: $cache"
    local chromium_dir
    chromium_dir="$(find "$cache" -maxdepth 1 -type d -name 'chromium-*' | head -n1 || true)"
    [ -n "$chromium_dir" ] || fail "playwright chromium binary missing under $cache"
    ok "playwright chromium: ${chromium_dir##*/}"
    # The probe uses the deterministic ``/opt/titan-probe`` install
    # instead of ``npx playwright-core``. ``npx`` interprets its
    # first positional argument as a binary to execute rather than
    # a package to install, so the previous ``npx playwright-core
    # node`` invocation was a no-op for the intended purpose.
    [ -d "$PROBE_NODE_MODULES/playwright-core" ] \
        || fail "playwright-core dependency missing from $PROBE_NODE_MODULES"
    local script="$PROBE_DIR/probe.js"
    cat > "$script" <<'JS'
const { chromium } = require('playwright-core');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent('<html><body><h1>titan-ci</h1></body></html>');
  const text = await page.locator('h1').textContent();
  if (text !== 'titan-ci') {
    console.error('unexpected rendered text:', text);
    process.exit(2);
  }
  await browser.close();
  console.log('chromium launched and rendered the smoke page');
})().catch((err) => {
  console.error(err);
  process.exit(3);
});
JS
    NODE_PATH="$PROBE_NODE_MODULES" \
    PLAYWRIGHT_BROWSERS_PATH="$cache" \
        node "$script" >&2 \
        || fail "playwright chromium launch failed"
    ok "playwright chromium launched headless and rendered smoke page"
}

probe_host_gateway() {
    # ``host.docker.internal`` must resolve to an IP address. The
    # alias is wired through ``extra_hosts`` on the listener (and
    # ``--add-host`` on the sidecars) so workflow service
    # containers and Compose-published HTTP services are reachable
    # without sharing the host network namespace.
    if ! getent hosts host.docker.internal >/dev/null 2>&1; then
        fail "host.docker.internal does not resolve; the host-gateway alias is missing"
    fi
    local address
    address="$(getent hosts host.docker.internal | awk '{print $1; exit}')"
    [ -n "$address" ] || fail "host.docker.internal resolved to an empty address"
    ok "host-gateway alias: host.docker.internal -> $address"
}

probe_network() {
    if curl --silent --show-error --max-time 8 -o /dev/null -w '%{http_code}\n' \
            https://api.github.com/repos/actions/runner/releases/latest 2>/dev/null \
            | grep -Eq '^(2|3)[0-9][0-9]$'; then
        ok "github api reachable"
    else
        fail "github api unreachable"
    fi
}

echo "titan-runner probe (native architecture: $EXPECTED_ARCH)"
titan-evidence --help >/dev/null
probe_docker
probe_compose
probe_buildx
probe_shellcheck
probe_gh
probe_node
probe_codex
probe_python
probe_postgres_client
probe_playwright
probe_host_gateway
if [ "${1:-}" != "--skip-network" ]; then
    probe_network
fi
echo "all capabilities verified"
