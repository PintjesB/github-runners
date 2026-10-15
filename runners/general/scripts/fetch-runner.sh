#!/usr/bin/env bash
# Fetch and verify the GitHub Actions runner tarball.
#
# This script runs at image build time. It maps BuildKit's automatic
# ``TARGETARCH`` build argument to the upstream archive naming
# (amd64 -> x64, arm64 -> arm64), selects the architecture-specific
# pinned digest, verifies it against the upstream tarball, and
# extracts the archive into ``/opt/actions-runner``. Every
# architecture other than amd64 and arm64 is refused before any
# download attempt so a wrong platform never reaches the network.
#
# The persistent listener symlinks that directory into the runner
# user's home on first start.
#
# The script is intentionally minimal: no GitHub API calls, no
# self-update, no background polling. Updating the runner requires
# rebuilding the image with a refreshed ``RUNNER_VERSION`` and the
# matching ``RUNNER_SHA256_ARM64`` / ``RUNNER_SHA256_X64`` digest
# pair. Persistent runners register with ``--disableupdate`` so even
# a compromised in-container update attempt cannot swap the binary.
#
# Environment variables:
#
#   TARGETARCH             Automatic BuildKit build argument. Must be
#                          ``amd64`` or ``arm64``; every other value
#                          aborts the script before any download.
#   RUNNER_VERSION         The pinned Actions runner version
#                          (e.g. 2.336.0).
#   RUNNER_SHA256_ARM64    The expected SHA-256 digest of the upstream
#                          ``actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz``
#                          tarball. Required when ``TARGETARCH=arm64``.
#   RUNNER_SHA256_X64      The expected SHA-256 digest of the upstream
#                          ``actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz``
#                          tarball. Required when ``TARGETARCH=amd64``.
#   RUNNER_TARBALL         Override the download URL. Defaults to the
#                          canonical GitHub release artifact for the
#                          resolved architecture.
#   RUNNER_DEST            Override the extraction directory. Defaults
#                          to ``/opt/actions-runner``.
#
# Exit codes:
#
#   0  Tarball downloaded, digest verified, archive extracted.
#   1  Required environment variable is missing or empty.
#   2  Download failed.
#   3  Digest verification failed.
#   4  Extraction failed.
#   5  Unsupported architecture.
set -euo pipefail

: "${RUNNER_VERSION:?RUNNER_VERSION is required (e.g. 2.336.0)}"
: "${TARGETARCH:?TARGETARCH is required (BuildKit build argument; must be amd64 or arm64)}"

DEST="${RUNNER_DEST:-/opt/actions-runner}"
TMP_DIR="$(mktemp -d -t actions-runner.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

log() { printf '[fetch-runner] %s\n' "$*"; }
fail() { log "ERROR: $*" >&2; exit "${2:-1}"; }

# Map BuildKit's automatic ``TARGETARCH`` build argument to the
# upstream archive name and to the architecture-specific pinned
# digest. Refuse every other architecture before any download
# attempt so a wrong platform never reaches the network.
case "$TARGETARCH" in
    amd64)
        ARCH_NAME="x64"
        : "${RUNNER_SHA256_X64:?RUNNER_SHA256_X64 is required when TARGETARCH=amd64}"
        expected="${RUNNER_SHA256_X64#sha256:}"
        ;;
    arm64)
        ARCH_NAME="arm64"
        : "${RUNNER_SHA256_ARM64:?RUNNER_SHA256_ARM64 is required when TARGETARCH=arm64}"
        expected="${RUNNER_SHA256_ARM64#sha256:}"
        ;;
    *)
        fail "unsupported architecture: TARGETARCH=$TARGETARCH (expected amd64 or arm64)" 5
        ;;
esac

TARBALL_URL="${RUNNER_TARBALL:-https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${ARCH_NAME}-${RUNNER_VERSION}.tar.gz}"
archive="$TMP_DIR/actions-runner.tar.gz"

log "target architecture : $TARGETARCH ($ARCH_NAME upstream archive)"
log "downloading $TARBALL_URL"
if ! curl --fail --silent --show-error --location --retry 3 --retry-delay 5 \
        --output "$archive" "$TARBALL_URL"; then
    fail "download failed" 2
fi

log "verifying sha256 digest"
computed="$(sha256sum "$archive" | awk '{print $1}')"
if [ "$computed" != "$expected" ]; then
    log "digest mismatch" >&2
    log "  expected: $expected" >&2
    log "  actual:   $computed" >&2
    fail "refusing to install upstream tarball" 3
fi

log "extracting to $DEST"
mkdir -p "$DEST"
if ! tar -xzf "$archive" -C "$DEST"; then
    fail "extraction failed" 4
fi

log "installed runner version: ${RUNNER_VERSION} (${ARCH_NAME})"
