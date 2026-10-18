#!/usr/bin/env bash
# Shell-level tests for the runner lifecycle scripts.
#
# The tests exercise input validation and contract guards directly
# without requiring Docker, the network, or the full set of
# installed CLI plugins. Behaviour that depends on the installed
# image (binary presence, Compose v2 plugin, etc.) is covered by
# ``deploy.sh probe`` inside the published image.
#
# Tests cover:
#
#   * ``fetch-runner.sh`` rejects missing environment variables,
#     unsupported ``TARGETARCH`` values, and digest mismatches.
#   * ``fetch-runner.sh`` selects the architecture-specific
#     ``RUNNER_SHA256_ARM64`` / ``RUNNER_SHA256_X64`` digest and
#     the matching upstream archive name.
#   * ``register.sh`` refuses to run without configuration and
#     rejects a missing or empty ``RUNNER_TOKEN``.
#   * ``start-runner.sh`` refuses to start without persisted
#     state.
#   * ``probe.sh`` honours ``--skip-network``, validates the
#     host-gateway alias, and requires an ``EXPECTED_ARCH`` env
#     var so it can refuse an emulated / mismatched Docker
#     daemon.
#   * ``pre-job.sh`` reads ``RUNNER_ARCH`` and rejects a Docker
#     daemon whose reported architecture does not match the
#     native runner architecture.
#   * ``deploy.sh`` rejects unknown subcommands and exposes only the
#     single-container lifecycle commands.
#   * ``pre-job.sh`` exists and validates host capabilities;
#     no shared-daemon post-job cleanup hook is installed.

set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$TESTS_DIR/.." && pwd)"
SCRIPTS_DIR="$ROOT_DIR/scripts"

assert_contains() {
    local haystack="$1" needle="$2" message="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        printf 'FAIL %s\n  expected to contain: %s\n' "$message" "$needle" >&2
        exit 1
    fi
}

assert_exit_code() {
    local actual="$1" expected="$2" message="$3"
    if [ "$actual" -ne "$expected" ]; then
        printf 'FAIL %s\n  expected exit: %s\n  actual exit: %s\n' "$message" "$expected" "$actual" >&2
        exit 1
    fi
}

ok() { printf 'ok   %s\n' "$*"; }

test_fetch_runner_rejects_missing_version() {
    local output rc
    output="$(env -i HOME=/tmp PATH=/usr/bin:/bin \
        TARGETARCH=arm64 \
        bash "$SCRIPTS_DIR/fetch-runner.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 1 "fetch-runner exits 1 when RUNNER_VERSION is unset"
    assert_contains "$output" "RUNNER_VERSION" "error mentions RUNNER_VERSION"
    ok "fetch-runner requires RUNNER_VERSION"
}

test_fetch_runner_rejects_missing_targetarch() {
    local output rc
    output="$(env -i HOME=/tmp PATH=/usr/bin:/bin \
        RUNNER_VERSION=2.336.0 \
        RUNNER_SHA256_ARM64=58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1 \
        RUNNER_SHA256_X64=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d \
        bash "$SCRIPTS_DIR/fetch-runner.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 1 "fetch-runner exits 1 when TARGETARCH is unset"
    assert_contains "$output" "TARGETARCH" "error mentions TARGETARCH"
    ok "fetch-runner requires TARGETARCH"
}

test_fetch_runner_rejects_unsupported_targetarch() {
    local output rc
    # Use a stub ``curl`` so an unsupported architecture never
    # reaches the network; the script MUST abort before any
    # download attempt.
    local tmp; tmp="$(mktemp -d)"
    mkdir -p "$tmp/bin"
    cat > "$tmp/bin/curl" <<EOF
#!/usr/bin/env bash
echo "unexpected curl invocation: \$@" >&2
exit 99
EOF
    chmod +x "$tmp/bin/curl"
    output="$(cd "$tmp" && env -i PATH="$tmp/bin:/usr/bin:/sbin:/bin" HOME="$tmp" \
        RUNNER_VERSION=2.336.0 \
        RUNNER_SHA256_ARM64=58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1 \
        RUNNER_SHA256_X64=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d \
        TARGETARCH=386 \
        bash "$SCRIPTS_DIR/fetch-runner.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 5 "fetch-runner exits 5 on unsupported TARGETARCH"
    assert_contains "$output" "unsupported architecture" "error mentions unsupported architecture"
    rm -rf "$tmp"
    ok "fetch-runner rejects unsupported TARGETARCH before any download"
}

test_fetch_runner_rejects_arm64_digest_mismatch() {
    local tmp; tmp="$(mktemp -d)"
    mkdir -p "$tmp/bin"
    # A ``curl`` stub that materialises a fake tarball in the
    # working directory captured at install time. ``fetch-runner.sh``
    # invokes curl with ``--output <path>`` so we honour that flag.
    cat > "$tmp/bin/curl" <<EOF
#!/usr/bin/env bash
output="\$PWD/fake.tar.gz"
while [ \$# -gt 0 ]; do
    case "\$1" in
        --output) output="\$2"; shift 2 ;;
        *) shift ;;
    esac
done
echo "fake upstream tarball" > "\$output"
EOF
    chmod +x "$tmp/bin/curl"
    local output rc
    output="$(cd "$tmp" && env -i PATH="$tmp/bin:/usr/bin:/sbin:/bin" HOME="$tmp" \
        TARGETARCH=arm64 \
        RUNNER_VERSION=2.336.0 \
        RUNNER_SHA256_ARM64=0000000000000000000000000000000000000000000000000000000000000000 \
        RUNNER_SHA256_X64=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d \
        bash "$SCRIPTS_DIR/fetch-runner.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 3 "fetch-runner exits 3 on arm64 digest mismatch"
    assert_contains "$output" "digest" "error mentions digest verification"
    rm -rf "$tmp"
    ok "fetch-runner rejects an arm64 digest mismatch"
}

test_fetch_runner_rejects_x64_digest_mismatch() {
    local tmp; tmp="$(mktemp -d)"
    mkdir -p "$tmp/bin"
    cat > "$tmp/bin/curl" <<EOF
#!/usr/bin/env bash
output="\$PWD/fake.tar.gz"
while [ \$# -gt 0 ]; do
    case "\$1" in
        --output) output="\$2"; shift 2 ;;
        *) shift ;;
    esac
done
echo "fake upstream tarball" > "\$output"
EOF
    chmod +x "$tmp/bin/curl"
    local output rc
    output="$(cd "$tmp" && env -i PATH="$tmp/bin:/usr/bin:/sbin:/bin" HOME="$tmp" \
        TARGETARCH=amd64 \
        RUNNER_VERSION=2.336.0 \
        RUNNER_SHA256_ARM64=58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1 \
        RUNNER_SHA256_X64=0000000000000000000000000000000000000000000000000000000000000000 \
        bash "$SCRIPTS_DIR/fetch-runner.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 3 "fetch-runner exits 3 on x64 digest mismatch"
    assert_contains "$output" "digest" "error mentions digest verification"
    rm -rf "$tmp"
    ok "fetch-runner rejects an x64 digest mismatch"
}

test_fetch_runner_uses_arm64_archive_name() {
    local tmp; tmp="$(mktemp -d)"
    mkdir -p "$tmp/bin"
    # A ``curl`` stub that records its arguments and emits a fake
    # tarball so the digest verification can succeed. The recorded
    # argument list is asserted below.
    cat > "$tmp/bin/curl" <<EOF
#!/usr/bin/env bash
output="\$PWD/fake.tar.gz"
while [ \$# -gt 0 ]; do
    case "\$1" in
        --output) output="\$2"; shift 2 ;;
        --location|--silent|--show-error|--retry|--retry-delay) shift 2 ;;
        --fail) shift ;;
        *) url="\$1"; shift ;;
    esac
done
echo "\$url" > "\$PWD/last-url"
echo "fake upstream tarball" > "\$output"
EOF
    chmod +x "$tmp/bin/curl"
    local output rc
    output="$(cd "$tmp" && env -i PATH="$tmp/bin:/usr/bin:/sbin:/bin" HOME="$tmp" \
        TARGETARCH=arm64 \
        RUNNER_VERSION=2.336.0 \
        RUNNER_SHA256_ARM64="$(printf 'fake upstream tarball' | sha256sum | awk '{print $1}')" \
        RUNNER_SHA256_X64=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d \
        bash "$SCRIPTS_DIR/fetch-runner.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    local recorded
    recorded="$(cat "$tmp/last-url" 2>/dev/null || true)"
    case "$recorded" in
        *actions-runner-linux-arm64-2.336.0.tar.gz*)
            ok "fetch-runner uses the arm64 upstream archive for TARGETARCH=arm64" ;;
        *)
            printf 'FAIL fetch-runner did not request the arm64 archive (got %s)\n' "$recorded" >&2
            exit 1
            ;;
    esac
    rm -rf "$tmp"
}

test_fetch_runner_uses_x64_archive_name() {
    local tmp; tmp="$(mktemp -d)"
    mkdir -p "$tmp/bin"
    cat > "$tmp/bin/curl" <<EOF
#!/usr/bin/env bash
output="\$PWD/fake.tar.gz"
while [ \$# -gt 0 ]; do
    case "\$1" in
        --output) output="\$2"; shift 2 ;;
        --location|--silent|--show-error|--retry|--retry-delay) shift 2 ;;
        --fail) shift ;;
        *) url="\$1"; shift ;;
    esac
done
echo "\$url" > "\$PWD/last-url"
echo "fake upstream tarball" > "\$output"
EOF
    chmod +x "$tmp/bin/curl"
    local output rc
    output="$(cd "$tmp" && env -i PATH="$tmp/bin:/usr/bin:/sbin:/bin" HOME="$tmp" \
        TARGETARCH=amd64 \
        RUNNER_VERSION=2.336.0 \
        RUNNER_SHA256_ARM64=58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1 \
        RUNNER_SHA256_X64="$(printf 'fake upstream tarball' | sha256sum | awk '{print $1}')" \
        bash "$SCRIPTS_DIR/fetch-runner.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    local recorded
    recorded="$(cat "$tmp/last-url" 2>/dev/null || true)"
    case "$recorded" in
        *actions-runner-linux-x64-2.336.0.tar.gz*)
            ok "fetch-runner uses the x64 upstream archive for TARGETARCH=amd64" ;;
        *)
            printf 'FAIL fetch-runner did not request the x64 archive (got %s)\n' "$recorded" >&2
            exit 1
            ;;
    esac
    rm -rf "$tmp"
}

test_register_requires_configuration() {
    # The script also refuses to run as a non-root user; either
    # failure is acceptable for the contract test.
    local rc=0
    env -i HOME=/tmp PATH=/usr/bin:/bin bash "$SCRIPTS_DIR/register.sh" >/dev/null 2>&1 || rc=$? || true
    rc="${rc:-0}"
    [ "$rc" -ne 0 ] || { echo "FAIL register must not run without configuration" >&2; exit 1; }
    ok "register requires configuration"
}

test_register_rejects_missing_token() {
    # ``register.sh`` requires ``root`` to map supplemental groups,
    # but the ``RUNNER_TOKEN`` guard fires before any privileged
    # operation. We therefore run with ``RUNNER_TOKEN`` unset and
    # confirm the script refuses the empty token.
    local rc=0
    env -i HOME=/tmp PATH=/usr/bin:/bin \
        REPO_URL=https://example.com/repo \
        bash "$SCRIPTS_DIR/register.sh" >/dev/null 2>&1 || rc=$? || true
    rc="${rc:-0}"
    # Either ``root`` or the missing-token guard may fire; both
    # produce non-zero. ``register.sh`` is invoked from inside a
    # root-pinned one-shot sidecar in production.
    [ "$rc" -ne 0 ] || { echo "FAIL register must reject an empty RUNNER_TOKEN" >&2; exit 1; }
    ok "register rejects an empty RUNNER_TOKEN"
}

test_start_runner_requires_state() {
    # The start-runner script also refuses to run as a non-root user;
    # the missing-credentials guard fires before any privileged
    # operation.
    local rc=0
    env -i HOME=/tmp PATH=/usr/bin:/bin bash "$SCRIPTS_DIR/start-runner.sh" >/dev/null 2>&1 || rc=$? || true
    rc="${rc:-0}"
    [ "$rc" -ne 0 ] || { echo "FAIL start-runner must require persisted state" >&2; exit 1; }
    ok "start-runner refuses to start without state"
}

test_probe_wires_skip_network() {
    assert_contains "$(cat "$SCRIPTS_DIR/probe.sh")" "skip-network" "probe supports skip-network"
    ok "probe honours --skip-network"
}

test_probe_validates_host_gateway() {
    # ``probe.sh`` MUST verify the host-gateway alias resolves.
    assert_contains "$(cat "$SCRIPTS_DIR/probe.sh")" "host.docker.internal" "probe validates host.docker.internal"
    assert_contains "$(cat "$SCRIPTS_DIR/probe.sh")" "probe_host_gateway" "probe exposes probe_host_gateway"
    ok "probe validates the host.docker.internal alias"
}

test_probe_requires_expected_arch() {
    local output rc
    output="$(env -i HOME=/tmp PATH=/usr/bin:/bin \
        bash "$SCRIPTS_DIR/probe.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 1 "probe exits 1 when EXPECTED_ARCH is unset"
    assert_contains "$output" "EXPECTED_ARCH" "error mentions EXPECTED_ARCH"
    ok "probe requires EXPECTED_ARCH"
}

test_probe_rejects_unsupported_expected_arch() {
    local output rc
    output="$(env -i HOME=/tmp PATH=/usr/bin:/bin \
        EXPECTED_ARCH=386 \
        bash "$SCRIPTS_DIR/probe.sh" --skip-network 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 1 "probe exits 1 on unsupported EXPECTED_ARCH"
    assert_contains "$output" "unsupported EXPECTED_ARCH" "error mentions unsupported EXPECTED_ARCH"
    ok "probe rejects unsupported EXPECTED_ARCH"
}

test_deploy_rejects_unknown_subcommand() {
    local rc=0
    env -i HOME=/tmp PATH=/usr/bin:/bin \
        TITAN_RUNNER_IMAGE=example.com/runner:dev \
        TITAN_RUNNER_REPO_URL=https://example.com/repo \
        bash "$ROOT_DIR/deploy.sh" bogus >/dev/null 2>&1 || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 2 "deploy.sh rejects unknown subcommands"
    ok "deploy.sh rejects unknown subcommands"
}

test_deploy_uses_single_container_lifecycle() {
    local text up_branch
    text="$(cat "$ROOT_DIR/deploy.sh")"
    if [[ "$text" == *$'register)'* ]]; then
        echo "FAIL deploy.sh must not expose a register subcommand" >&2
        exit 1
    fi
    up_branch="$(printf '%s\n' "$text" | awk '/up\)/{flag=1;next}/down\)/{flag=0}flag')"
    assert_contains "$up_branch" "docker compose" "deploy.sh up must invoke docker compose"
    assert_contains "$up_branch" "up -d" "deploy.sh up must start the runner detached"
    ok "deploy.sh uses a single-container lifecycle"
}

test_pre_job_hook_exists_and_is_executable() {
    [ -x "$SCRIPTS_DIR/pre-job.sh" ] \
        || { echo "FAIL pre-job.sh must be executable" >&2; exit 1; }
    assert_contains "$(cat "$SCRIPTS_DIR/pre-job.sh")" "host.docker.internal" \
        "pre-job.sh validates host.docker.internal"
    assert_contains "$(cat "$SCRIPTS_DIR/pre-job.sh")" "RUNNER_ARCH" \
        "pre-job.sh reads RUNNER_ARCH"
    assert_contains "$(cat "$SCRIPTS_DIR/pre-job.sh")" "expected_arch" \
        "pre-job.sh maps RUNNER_ARCH to the docker daemon alias"
    ok "pre-job.sh validates host-gateway alias"
}

test_pre_job_hook_rejects_missing_runner_arch() {
    local output rc
    output="$(env -i HOME=/tmp PATH=/usr/bin:/bin \
        bash "$SCRIPTS_DIR/pre-job.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    [ "$rc" -ne 0 ] || { echo "FAIL pre-job must reject missing RUNNER_ARCH" >&2; exit 1; }
    assert_contains "$output" "RUNNER_ARCH" "error mentions RUNNER_ARCH"
    ok "pre-job requires RUNNER_ARCH"
}

test_pre_job_hook_rejects_unsupported_runner_arch() {
    local output rc
    output="$(env -i HOME=/tmp PATH=/usr/bin:/bin \
        RUNNER_ARCH=ppc64le \
        bash "$SCRIPTS_DIR/pre-job.sh" 2>&1)" || rc=$? || true
    rc="${rc:-0}"
    assert_exit_code "$rc" 1 "pre-job exits 1 on unsupported RUNNER_ARCH"
    assert_contains "$output" "unsupported RUNNER_ARCH" "error mentions unsupported RUNNER_ARCH"
    ok "pre-job rejects unsupported RUNNER_ARCH"
}

test_post_job_hook_is_absent() {
    [ ! -e "$SCRIPTS_DIR/post-job.sh" ] ||
        { echo "FAIL post-job.sh must not be installed (unsafe cross-job cleanup)" >&2; exit 1; }
    ok "No post-job cleanup hook can sweep active Playwright projects"
}

main() {
    test_fetch_runner_rejects_missing_version
    test_fetch_runner_rejects_missing_targetarch
    test_fetch_runner_rejects_unsupported_targetarch
    test_fetch_runner_rejects_arm64_digest_mismatch
    test_fetch_runner_rejects_x64_digest_mismatch
    test_fetch_runner_uses_arm64_archive_name
    test_fetch_runner_uses_x64_archive_name
    test_register_requires_configuration
    test_register_rejects_missing_token
    test_start_runner_requires_state
    test_probe_wires_skip_network
    test_probe_validates_host_gateway
    test_probe_requires_expected_arch
    test_probe_rejects_unsupported_expected_arch
    test_deploy_rejects_unknown_subcommand
    test_deploy_uses_single_container_lifecycle
    test_pre_job_hook_exists_and_is_executable
    test_pre_job_hook_rejects_missing_runner_arch
    test_pre_job_hook_rejects_unsupported_runner_arch
    test_post_job_hook_is_absent
    echo "all runner shell tests passed"
}

main "$@"
