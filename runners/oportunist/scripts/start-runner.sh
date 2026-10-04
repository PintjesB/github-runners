#!/usr/bin/env bash
set -euo pipefail

log() { printf '[start-runner] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit "${2:-1}"; }

[ "$(id -u)" -eq 0 ] || fail "start-runner must run as root"

RUNNER_STATE_DIR="${RUNNER_STATE_DIR:-/var/lib/oportunist-runner/state}"
RUNNER_RUNTIME_DIR="${RUNNER_RUNTIME_DIR:-/var/lib/oportunist-runner/runtime}"
RUNNER_WORK_DIR="${RUNNER_WORK_DIR:-/var/lib/oportunist-runner/work}"
RUNNER_ROOT="${RUNNER_ROOT:-/opt/actions-runner}"
CODEX_HOME="${CODEX_HOME:-/home/runner/.codex}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-/var/lib/oportunist-runner/cache/pip}"
npm_config_cache="${npm_config_cache:-/var/lib/oportunist-runner/cache/npm}"
export PIP_CACHE_DIR npm_config_cache
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
    "$RUNNER_STATE_DIR" "$RUNNER_RUNTIME_DIR" "$RUNNER_WORK_DIR" "$CODEX_HOME" \
    "$PIP_CACHE_DIR" "$npm_config_cache"
chown -R runner:runner "$CODEX_HOME"

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
exec env HOME="$RUNNER_RUNTIME_DIR" CODEX_HOME="$CODEX_HOME" \
    gosu runner "$RUNNER_RUNTIME_DIR/run.sh"
