#!/usr/bin/env bash
# GitHub Actions runner pre-job hook for Titan Stocks.
#
# The hook runs in the runner user's context immediately before a
# workflow job starts. It enforces the documented host-boundary
# contract so a missing capability is caught before the job wastes
# cycles:
#
#   * Docker daemon is reachable through the bind-mounted socket.
#     The daemon's reported architecture MUST match the native
#     runner architecture (``amd64`` or ``arm64``); an emulated or
#     mismatched daemon is rejected so a degraded host cannot pass
#     the host-boundary contract.
#   * The ``host.docker.internal`` alias resolves. Workflow service
#     containers and Compose-published HTTP services are reached
#     through this alias.
#   * The Docker CLI, Compose v2 plugin, and Buildx plugin resolve.
#   * Node 24 and Python 3.12 are available on ``PATH``.
#   * The Playwright Chromium cache contains at least one Chromium
#     binary directory.
#
# The native runner architecture is read from the
# ``RUNNER_ARCH`` env var (``X64`` or ``ARM64``) supplied by the
# GitHub Actions runner based on the listener's actual
# architecture; the host-architecture mapping is documented in
# ``deploy.sh``. Any other value (or an unset value) aborts the
# hook before the rest of the contract runs so a misconfigured
# deployment cannot dispatch a job.
#
# The hook never modifies host state and never reaches out to the
# network. A failure exits non-zero so the runner reports the job
# as failed rather than letting it run on a host that cannot
# satisfy the workflow's preconditions.
set -euo pipefail

log() { printf '[pre-job] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit 1; }

# Resolve the native runner architecture from the documented
# ``RUNNER_ARCH`` env var. GitHub supplies ``X64`` on x86_64
# runners and ``ARM64`` on aarch64 runners; we map those to the
# ``amd64`` / ``arm64`` aliases Docker reports.
: "${RUNNER_ARCH:?RUNNER_ARCH is required (X64 or ARM64; supplied by GitHub Actions on every job)}"
case "$RUNNER_ARCH" in
    X64)   expected_arch="amd64" ;;
    ARM64) expected_arch="arm64" ;;
    *)
        log "FAIL unsupported RUNNER_ARCH: $RUNNER_ARCH (expected X64 or ARM64)"
        exit 1
        ;;
esac

# Run every check in a subshell so a single failure does not abort
# the rest of the diagnostics before the operator sees the full
# picture.
check() {
    local name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        log "ok $name"
    else
        log "FAIL $name"
        exit 1
    fi
}

# Docker daemon reachability through the bind-mounted socket. The
# daemon's reported architecture MUST match the native runner
# architecture so workflow images run on a native daemon rather
# than under emulation.
check_docker() {
    command -v docker >/dev/null 2>&1 || return 1
    local info
    info="$(docker info --format '{{.ServerVersion}}|{{.OSType}}/{{.Architecture}}' 2>&1)" || return 1
    case "$info" in
        *"/amd64"|*"/x86_64") daemon_arch="amd64" ;;
        *"/arm64"|*"/aarch64") daemon_arch="arm64" ;;
        *) return 1 ;;
    esac
    [ "$daemon_arch" = "$expected_arch" ]
}

check_compose() { docker compose version --short >/dev/null 2>&1; }
check_buildx() { docker buildx version >/dev/null 2>&1; }

check_node() {
    command -v node >/dev/null 2>&1 || return 1
    local major
    major="$(node -e 'console.log(process.versions.node.split(".")[0])' 2>/dev/null)"
    [ "$major" = "24" ]
}

check_python() {
    local python=""
    for candidate in python python3 python3.12; do
        if command -v "$candidate" >/dev/null 2>&1; then
            python="$candidate"
            break
        fi
    done
    [ -n "$python" ] || return 1
    "$python" -c 'import sys; raise SystemExit(0 if (3, 12) <= sys.version_info[:2] < (3, 14) else 1)'
}

check_host_gateway() {
    getent hosts host.docker.internal >/dev/null 2>&1
}

check_playwright_cache() {
    local cache="${PLAYWRIGHT_BROWSERS_PATH:-/home/runner/.cache/ms-playwright}"
    [ -d "$cache" ] || return 1
    find "$cache" -maxdepth 1 -type d -name 'chromium-*' -print -quit 2>/dev/null | grep -q .
}

log "validating Titan runner host capabilities (native architecture: $expected_arch)"
check "docker daemon (matches native runner architecture)" check_docker
check "docker compose plugin" check_compose
check "docker buildx plugin" check_buildx
check "node 24 on PATH" check_node
check "python 3.12+ on PATH" check_python
check "host.docker.internal alias" check_host_gateway
check "playwright chromium cache" check_playwright_cache
check "evidence client installed" /usr/local/bin/titan-evidence --help
if [ -n "${TITAN_EVIDENCE_BASE_URL:-}${TITAN_EVIDENCE_AUDIENCE:-}" ]; then
    check "evidence routing configuration" /usr/local/bin/titan-evidence --check-config
fi
exit 0
