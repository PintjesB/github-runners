#!/usr/bin/env bash
set -euo pipefail

log() { printf '[start-runner] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit "${2:-1}"; }

[ "$(id -u)" -eq 0 ] || fail "start-runner must run as root"

RUNNER_STATE_DIR="${RUNNER_STATE_DIR:-/var/lib/general-runner/state}"
RUNNER_RUNTIME_DIR="${RUNNER_RUNTIME_DIR:-/var/lib/general-runner/runtime}"
RUNNER_WORK_DIR="${RUNNER_WORK_DIR:-/var/lib/general-runner/work}"
RUNNER_ROOT="${RUNNER_ROOT:-/opt/actions-runner}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-/var/lib/general-runner/cache/pip}"
npm_config_cache="${npm_config_cache:-/var/lib/general-runner/cache/npm}"
export PIP_CACHE_DIR npm_config_cache RUNNER_WORK_DIR RUNNER_RUNTIME_DIR
DOCKER_SOCKET="${DOCKER_SOCKET:-/var/run/docker.sock}"

trap 'unset RUNNER_TOKEN || true' EXIT HUP INT TERM
/usr/local/bin/register
unset RUNNER_TOKEN

for path in "$RUNNER_STATE_DIR/.runner" "$RUNNER_STATE_DIR/.credentials"; do
    [ -s "$path" ] || fail "missing persisted runner state: $path"
done

if [ -S "$DOCKER_SOCKET" ]; then
    docker_gid="$(stat -c '%g' "$DOCKER_SOCKET")"
    if ! getent group "$docker_gid" >/dev/null 2>&1; then
        groupadd --gid "$docker_gid" docker-host
    fi
    group_name="$(getent group "$docker_gid" | cut -d: -f1)"
    if ! id -Gn runner | tr ' ' '\n' | grep -qx "$group_name"; then
        usermod -a -G "$docker_gid" runner
    fi
fi

install -d -m 0750 -o runner -g runner \
    "$RUNNER_STATE_DIR" "$RUNNER_RUNTIME_DIR" "$RUNNER_WORK_DIR" \
    "$PIP_CACHE_DIR" "$npm_config_cache"

# Stop stale PostgreSQL first. Its data is deliberately outside the normal
# workspace scrub so a failed stop can never cause live PGDATA to be removed.
if [ -x /usr/local/bin/runner-ci-postgres ]; then
    if ! timeout --signal=TERM --kill-after=10s 60s gosu runner /usr/local/bin/runner-ci-postgres stop; then
        log "WARNING: PostgreSQL startup recovery failed; the pre-job hook will retry"
    fi
fi

# Reclaim disposable work/cache volumes before copying runner binaries.
# This ordering matters if Docker restarted because the filesystem was full.
# The state volume is never touched, so registration remains persistent.
log "cleaning disposable job data before runner-runtime initialization"
if ! timeout --signal=TERM --kill-after=10s 300s gosu runner bash /opt/runner-hooks/runner-job-cleanup.sh startup; then
    log "WARNING: startup cleanup failed or timed out; job-started hook will retry and gate new jobs"
fi

rm -rf "$RUNNER_RUNTIME_DIR"
mkdir -p "$RUNNER_RUNTIME_DIR"
cp -a "$RUNNER_ROOT/." "$RUNNER_RUNTIME_DIR/"
cp "$RUNNER_STATE_DIR/.runner" "$RUNNER_RUNTIME_DIR/.runner"
cp "$RUNNER_STATE_DIR/.credentials" "$RUNNER_RUNTIME_DIR/.credentials"
if [ -f "$RUNNER_STATE_DIR/.credentials_rsaparams" ]; then
    cp "$RUNNER_STATE_DIR/.credentials_rsaparams" "$RUNNER_RUNTIME_DIR/.credentials_rsaparams"
fi
chown -R runner:runner "$RUNNER_RUNTIME_DIR"
chmod 0640 "$RUNNER_RUNTIME_DIR/.runner"
chmod 0600 "$RUNNER_RUNTIME_DIR"/.credentials* 2>/dev/null || true

log "launching persistent listener"
exec env HOME="$RUNNER_RUNTIME_DIR" \
    gosu runner "$RUNNER_RUNTIME_DIR/run.sh"
