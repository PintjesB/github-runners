#!/usr/bin/env bash
# Start the persistent Titan Stocks GitHub Actions listener.
#
# Architecture
# ============
#
# The image-owned tree, persistent state, and disposable runtime
# cooperate at startup. Registration and listening share this one
# container, while persisted identity means ordinary restarts do not
# need a new registration token.
#
#   /opt/actions-runner         Image-owned immutable binary tree
#                              (e.g. ``run.sh``, ``config.sh``,
#                              ``runsvc.sh``).
#
#   /var/lib/titan-runner/state    Persistent named volume. Holds
#                              the Actions runner ``.runner`` and
#                              ``.credentials*`` files.
#
#   /var/lib/titan-runner/runtime  Materialised tree built fresh on
#                              every container start by copying the
#                              immutable image tree and overlaying
#                              the persistent state. The listener
#                              reads its configuration from here and
#                              writes its logs here. A container
#                              recreation can discard the runtime
#                              completely without losing the GitHub
#                              identity.
#
#   /var/lib/titan-runner/work     Persistent ``titan-runner-work``
#                              named volume used as GitHub's ``_work``
#                              directory. Child job containers mount the
#                              same external volume at this fixed path.
#
#   /var/lib/titan-runner/browser  Persistent named volume. Holds
#                              the Playwright Chromium browser cache.
#                              Seeded from the baked image cache
#                              ``/home/runner/.cache/ms-playwright``
#                              on startup when an image-required revision
#                              is missing; existing revisions are preserved.
#
#   /home/runner/.codex        Persistent ``titan-runner-codex``
#                              named volume. Holds replaceable Codex
#                              authentication state. Losing this volume
#                              only requires another device login.
#
#   /opt/titan-probe/node_modules  Image-owned ``playwright-core``
#                              dependency tree. The capability probe
#                              resolves ``NODE_PATH`` onto this
#                              directory instead of running
#                              ``npx playwright-core`` at probe time.
#
# On every start the script:
#
#   1. Runs the idempotent ``register`` phase in this same container.
#      A matching persisted identity exits immediately; a fresh or
#      changed identity requires ``RUNNER_TOKEN``. The token is unset
#      before the listener is launched.
#   2. Refuses to run if registration did not leave a non-empty
#      ``state/.runner`` and ``state/.credentials``.
#   3. Grants the host Docker socket's GID to the runner user as a
#      *supplemental* group; the primary ``runner`` group is
#      preserved.
#   4. Ensures all persistent directories, including ``CODEX_HOME``,
#      are owned by the non-root ``runner`` user.
#   5. Rebuilds the runtime tree from ``/opt/actions-runner`` and
#      overlays the persisted registration files onto it.
#   6. Seeds the persistent browser volume from the baked image
#      cache, adding missing image-required revisions while preserving
#      existing complete revisions.
#   7. Launches ``run.sh`` directly via ``gosu`` with no runtime
#      flags. ``--disableupdate`` is set at registration and
#      persists in the ``.runner`` manifest.
#
# Environment variables
# =====================
#
#   RUNNER_STATE_DIR    Persistent state volume. Default
#                       ``/var/lib/titan-runner/state``.
#   RUNNER_RUNTIME_DIR  Disposable runtime tree. Default
#                       ``/var/lib/titan-runner/runtime``.
#   RUNNER_WORK_DIR     Fixed ``_work`` path in the persistent
#                       ``titan-runner-work`` volume. Default
#                       ``/var/lib/titan-runner/work``.
#   RUNNER_BROWSER_DIR  Persistent Playwright cache. Default
#                       ``/var/lib/titan-runner/browser``.
#   RUNNER_BROWSER_SEED Baked image cache the persistent volume is
#                       seeded from for missing revisions. Default
#                       ``/home/runner/.cache/ms-playwright``.
#   RUNNER_ROOT         Image-owned source tree. Default
#                       ``/opt/actions-runner``.
#   CODEX_HOME          Persistent Codex configuration/auth path.
#                       Default ``/home/runner/.codex``.
#   RUNNER_TOKEN        Short-lived GitHub registration token. It is
#                       consumed by ``register`` and unset before the
#                       listener process starts.
#
# Exit codes
# ==========
#
#   0  Listener exited cleanly.
#   1  Required configuration missing or invalid.
#   2  Registration token missing for a new or changed identity.
#   3  GitHub runner configuration failed.
#   4  Listener exited non-zero.
set -euo pipefail

log() { printf '[start-runner] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit "${2:-1}"; }

if [ "$(id -u)" -ne 0 ]; then
    fail "start-runner must run as root (the entrypoint sets up the runner user)" 1
fi

RUNNER_STATE_DIR="${RUNNER_STATE_DIR:-/var/lib/titan-runner/state}"
RUNNER_RUNTIME_DIR="${RUNNER_RUNTIME_DIR:-/var/lib/titan-runner/runtime}"
RUNNER_WORK_DIR="${RUNNER_WORK_DIR:-/var/lib/titan-runner/work}"
RUNNER_BROWSER_DIR="${RUNNER_BROWSER_DIR:-/var/lib/titan-runner/browser}"
RUNNER_BROWSER_SEED="${RUNNER_BROWSER_SEED:-/home/runner/.cache/ms-playwright}"
RUNNER_ROOT="${RUNNER_ROOT:-/opt/actions-runner}"
CODEX_HOME="${CODEX_HOME:-/home/runner/.codex}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-/var/lib/titan-runner/cache/pip}"
npm_config_cache="${npm_config_cache:-/var/lib/titan-runner/cache/npm}"
export CODEX_HOME PIP_CACHE_DIR npm_config_cache

# The Compose service is the only long-lived container. Registration is
# an internal startup phase, and these traps ensure the short-lived
# token is never retained by the shell if registration or startup fails.
cleanup_token() { unset RUNNER_TOKEN || true; }
trap cleanup_token EXIT
trap 'cleanup_token; exit 129' HUP
trap 'cleanup_token; exit 130' INT
trap 'cleanup_token; exit 143' TERM

log "ensuring persistent GitHub runner registration"
/usr/local/bin/register
unset RUNNER_TOKEN

# Registration must have produced complete persisted state. This is a
# defensive check for a successful-but-incomplete registration script.
for path in \
    "$RUNNER_STATE_DIR/.runner" \
    "$RUNNER_STATE_DIR/.credentials"; do
    if [ ! -s "$path" ]; then
        fail "missing persisted credential file after registration: $path" 1
    fi
done

if [ ! -x "$RUNNER_ROOT/run.sh" ]; then
    fail "image-owned runner binaries missing from $RUNNER_ROOT; rebuild the image" 1
fi

# Re-apply the host Docker socket's GID to the runner user as a
# supplemental group on every start so a host GID change survives a
# container recreation. The primary ``runner`` group is preserved.
DOCKER_SOCKET="${DOCKER_SOCKET:-/var/run/docker.sock}"
if [ -S "$DOCKER_SOCKET" ]; then
    host_docker_gid="$(stat -c '%g' "$DOCKER_SOCKET" 2>/dev/null || stat -f '%g' "$DOCKER_SOCKET" 2>/dev/null || echo "")"
    if [ -n "$host_docker_gid" ]; then
        if ! getent group "$host_docker_gid" >/dev/null 2>&1; then
            groupadd --gid "$host_docker_gid" docker-host || fail "could not create host docker group" 1
        fi
        group_name="$(getent group "$host_docker_gid" | awk -F: '{print $1}')"
        if [ -n "$group_name" ] && ! id -Gn runner | tr ' ' '\n' | grep -qx "$group_name"; then
            log "adding runner user to supplemental group GID=$host_docker_gid"
            usermod -a -G "$host_docker_gid" runner
        fi
    fi
else
    log "warning: $DOCKER_SOCKET is not a socket; Docker CLI calls will fail inside the runner"
fi

# Ensure the persistent directories exist and are owned by runner. The
# Codex named volume may be brand new after deployment or after an
# operator deliberately removes it, so ownership is repaired on every
# container start before any workflow can invoke ``codex``.
install -d -m 0750 -o runner -g runner \
    "$RUNNER_STATE_DIR" \
    "$RUNNER_RUNTIME_DIR" \
    "$RUNNER_WORK_DIR" \
    "$RUNNER_BROWSER_DIR" \
    "$PIP_CACHE_DIR" \
    "$npm_config_cache" \
    "$CODEX_HOME"
chown -R runner:runner "$CODEX_HOME"

# Rebuild the runtime tree from the immutable image tree. The runner
# reads ``.runner`` and ``.credentials*`` from ``$HOME`` at startup,
# so setting ``HOME=$RUNNER_RUNTIME_DIR`` makes the overlaid
# credentials resolve naturally.
log "materialising runtime tree at $RUNNER_RUNTIME_DIR"
rm -rf "$RUNNER_RUNTIME_DIR"
mkdir -p "$RUNNER_RUNTIME_DIR"
cp -a "$RUNNER_ROOT/." "$RUNNER_RUNTIME_DIR/"

# Overlay persisted registration files onto the runtime tree. The
# upstream tooling requires ``$HOME/.runner`` and
# ``$HOME/.credentials`` to exist.
cp "$RUNNER_STATE_DIR/.runner" "$RUNNER_RUNTIME_DIR/.runner"
cp "$RUNNER_STATE_DIR/.credentials" "$RUNNER_RUNTIME_DIR/.credentials"
if [ -f "$RUNNER_STATE_DIR/.credentials_rsaparams" ]; then
    cp "$RUNNER_STATE_DIR/.credentials_rsaparams" "$RUNNER_RUNTIME_DIR/.credentials_rsaparams"
fi

chown -R runner:runner "$RUNNER_RUNTIME_DIR"
chmod 0640 "$RUNNER_RUNTIME_DIR/.runner"
chmod 0600 "$RUNNER_RUNTIME_DIR/.credentials" \
          "$RUNNER_RUNTIME_DIR/.credentials_rsaparams" 2>/dev/null || true

# Merge image-required Playwright revisions into the persistent cache.
# Publish complete directories with an atomic rename on the same volume;
# an interrupted copy must never leave a partially seeded final revision.
seed_browser_cache() {
    local seed="$1" dest="$2" entry name target staging link
    [ -d "$seed" ] || return 0
    for entry in "$seed"/* "$seed"/.links; do
        [ -d "$entry" ] || continue
        name="${entry##*/}"
        target="$dest/$name"
        if [ "$name" = .links ]; then
            # Preserve other installations' registrations. The image probe's
            # link prevents Playwright GC from discarding its required browsers.
            mkdir -p "$target"
            for link in "$entry"/*; do
                [ -f "$link" ] || continue
                [ ! -e "$target/${link##*/}" ] || continue
                staging="$(mktemp "$target/.seed.XXXXXX")"
                if ! cp -a "$link" "$staging"; then
                    rm -f "$staging"
                    return 1
                fi
                mv "$staging" "$target/${link##*/}"
            done
            continue
        fi
        if [ ! -f "$entry/INSTALLATION_COMPLETE" ]; then
            log "ERROR: image browser revision is incomplete: $entry"
            return 1
        fi
        if [ -e "$target" ]; then
            if [ ! -d "$target" ] || [ ! -f "$target/INSTALLATION_COMPLETE" ]; then
                log "ERROR: existing browser revision is incomplete; preserve and inspect: $target"
                return 1
            fi
            continue
        fi
        log "seeding missing Playwright browser revision: $name"
        staging="$(mktemp -d "$dest/.seed.XXXXXX")"
        if ! cp -a "$entry/." "$staging/"; then
            rm -rf "$staging"
            return 1
        fi
        mv "$staging" "$target"
    done
}
seed_browser_cache "$RUNNER_BROWSER_SEED" "$RUNNER_BROWSER_DIR"
chown -R runner:runner "$RUNNER_BROWSER_DIR"

log "launching persistent listener; foreground logs follow"
if ! env \
        HOME="$RUNNER_RUNTIME_DIR" \
        PLAYWRIGHT_BROWSERS_PATH="$RUNNER_BROWSER_DIR" \
        CODEX_HOME="$CODEX_HOME" \
        gosu runner \
        "$RUNNER_RUNTIME_DIR/run.sh"; then
    fail "listener exited non-zero" 4
fi
