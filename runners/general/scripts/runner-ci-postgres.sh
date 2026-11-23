#!/usr/bin/env bash
# Disposable PostgreSQL 16 server for a persistent general-runner listener.
# A workflow step calls 'runner-ci-postgres start'; job hooks call 'stop'.
# This tool never registers runners, accesses Docker or touches runner state.
set -euo pipefail
umask 077

log() { printf '[runner-ci-postgres] %s\n' "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

action="${1:-}"
case "$action" in start|stop|status) ;; *) fail 'usage: runner-ci-postgres start|stop|status' ;; esac
[ "$(id -u)" -ne 0 ] || fail "PostgreSQL must run as the unprivileged runner user"
[ "${PG_MAJOR:-16}" = 16 ] || fail "general-runner requires PostgreSQL major 16"
PG_BIN=/usr/lib/postgresql/16/bin
WORK="${RUNNER_WORK_DIR:-/var/lib/general-runner/work}"
case "$WORK" in
    /var/lib/general-runner/work|/var/lib/oportunist-runner/work) ;;
    /tmp/*) ;; # temporary local fixtures; still restricted by realpath and ownership
    *) fail "unrecognized runner work root $WORK" ;;
esac
[ -d "$WORK" ] && [ ! -L "$WORK" ] && [ -O "$WORK" ] ||
    fail "runner work root is not a safe owned directory: $WORK"
[ "$(realpath -e -- "$WORK")" = "$WORK" ] ||
    fail "runner work root includes symlinked parents: $WORK"

BASE="$WORK/_ci_postgres"
PGDATA="$BASE/data"
LOCK="$WORK/_ci_postgres.lock"

# Never traverse user-supplied symlinks or nested bind mounts, including
# mounts on the same filesystem as Docker's named work volume.
check_mounts() {
    local path="$1" mount
    command -v findmnt >/dev/null || fail "findmnt unavailable"
    local mounts
    mounts="$(findmnt -rn -o TARGET)" || fail "could not inspect mount table"
    while IFS= read -r mount; do
        case "$mount" in "$path"|"$path"/*) fail "refusing mounted PostgreSQL path: $mount" ;; esac
    done <<< "$mounts"
}
check_base() {
    if [ -e "$BASE" ] || [ -L "$BASE" ]; then
        [ -d "$BASE" ] && [ ! -L "$BASE" ] && [ -O "$BASE" ] ||
            fail "unsafe existing PostgreSQL directory $BASE"
        [ "$(realpath -e -- "$BASE")" = "$BASE" ] ||
            fail "symlinked PostgreSQL directory $BASE"
    fi
    check_mounts "$BASE"
}
check_base

[ ! -L "$LOCK" ] || fail "unsafe PostgreSQL lock symlink"
if [ ! -e "$LOCK" ]; then
    (set -C; : > "$LOCK") 2>/dev/null || fail "cannot create PostgreSQL lock"
fi
[ -f "$LOCK" ] && [ -O "$LOCK" ] && [ ! -L "$LOCK" ] ||
    fail "unsafe PostgreSQL lock"
exec 9<>"$LOCK"
flock -x 9 || fail "cannot acquire PostgreSQL lock"

# Prevent deleting a directory containing an unexpected mounted dataset.
stop_locked() {
    check_base
    [ -d "$BASE" ] || return 0
    if [ -d "$PGDATA" ]; then
        [ ! -L "$PGDATA" ] && [ -O "$PGDATA" ] ||
            fail "unsafe PostgreSQL data directory"
        if [ -x "$PG_BIN/pg_ctl" ]; then
            "$PG_BIN/pg_ctl" -D "$PGDATA" -m immediate -w -t 30 stop >/dev/null 2>&1 || true
            if "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
                fail "PostgreSQL is still running; refusing to remove data"
            fi
        elif [ -f "$PGDATA/postmaster.pid" ]; then
            fail "cannot stop PostgreSQL: pg_ctl is unavailable"
        fi
    fi
    rm -rf --one-file-system -- "$BASE" || fail "could not remove PostgreSQL job data"
    [ ! -e "$BASE" ] || fail "PostgreSQL job data remains after cleanup"
    log "stopped and removed disposable cluster"
}

if [ "$action" = stop ]; then
    stop_locked
    exit 0
fi
if [ "$action" = status ]; then
    if [ -d "$PGDATA" ] && [ -x "$PG_BIN/pg_ctl" ] &&
       "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
        log "running on loopback port $(cat "$BASE/port" 2>/dev/null || echo unknown)"
        exit 0
    fi
    log "not running"
    exit 1
fi

# Start only when requested by a workflow. No PostgreSQL daemon is started
# when the listener container starts or for jobs that do not use PostgreSQL.
[ -x "$PG_BIN/initdb" ] && [ -x "$PG_BIN/pg_ctl" ] &&
[ -x "$PG_BIN/postgres" ] && [ -x "$PG_BIN/psql" ] ||
    fail "PostgreSQL 16 server/client binaries are not installed"
case "$("$PG_BIN/postgres" --version)" in *" 16."*) ;; *) fail "wrong PostgreSQL server version" ;; esac
locale -a | grep -Eiq '^en_US\.utf8$' || fail "en_US.UTF-8 locale is not installed"

DB="${RUNNER_CI_POSTGRES_DB:-ci_test}"
DB_USER="${RUNNER_CI_POSTGRES_USER:-ci}"
[[ "$DB" =~ ^[a-z_][a-z_0-9]*$ ]] || fail "invalid database name"
[[ "$DB_USER" =~ ^[a-z_][a-z_0-9]*$ ]] || fail "invalid database user"

stop_locked
mkdir -m 0700 -- "$BASE" || fail "cannot create PostgreSQL directory"
password="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
password_file="$BASE/password"
printf '%s\n' "$password" > "$password_file"

# If a job is interrupted before start completes, clean this attempt.
ready=0
on_exit() {
    local rc=$?
    if [ "$ready" -ne 1 ]; then
        stop_locked || true
    fi
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$PG_BIN/initdb" -D "$PGDATA" -U "$DB_USER" --pwfile="$password_file" \
    --auth-host=scram-sha-256 --auth-local=reject \
    --encoding=UTF8 --locale=en_US.UTF-8 >/dev/null
rm -f "$password_file"

opts="-c listen_addresses=127.0.0.1 -c unix_socket_directories= -c fsync=off"
opts+=" -c synchronous_commit=off -c full_page_writes=off"
opts+=" -c timezone=UTC -c log_timezone=UTC"

started=0
for attempt in 1 2 3 4 5; do
    port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    # pg_ctl forks a daemon. Close the lock fd in the child so postgres
    # cannot retain our flock after this command exits.
    if "$PG_BIN/pg_ctl" -D "$PGDATA" -w -t 30 -l "$BASE/postgres.log" \
        -o "$opts -c port=$port" start 9>&- >/dev/null 2>&1; then
        started=1
        break
    fi
    log "PostgreSQL start attempt $attempt did not become ready"
done
if [ "$started" -ne 1 ]; then
    log "PostgreSQL failed to start after five attempts"
    tail -n 40 "$BASE/postgres.log" >&2 2>/dev/null || true
    exit 1
fi

conn=(-h 127.0.0.1 -p "$port" -U "$DB_USER" -X -v ON_ERROR_STOP=1 -At)
PGPASSWORD="$password" "$PG_BIN/psql" "${conn[@]}" -d postgres \
    -c "CREATE DATABASE \"$DB\"" >/dev/null
listen="$(PGPASSWORD="$password" "$PG_BIN/psql" "${conn[@]}" -d "$DB" -c 'SHOW listen_addresses')"
sockets="$(PGPASSWORD="$password" "$PG_BIN/psql" "${conn[@]}" -d "$DB" -c 'SHOW unix_socket_directories')"
[ "$listen" = 127.0.0.1 ] && [ -z "$sockets" ] ||
    fail "PostgreSQL is not restricted to TCP loopback"

url="postgresql://$DB_USER:$password@127.0.0.1:$port/$DB"
printf '%s\n' "$port" > "$BASE/port"
printf 'export CI_POSTGRES_URL=%q\nexport POSTGRES_TEST_URL=%q\n' "$url" "$url" \
    > "$BASE/connection.env"
if [ -n "${GITHUB_ACTIONS:-}" ]; then
    echo "::add-mask::$password"
fi
if [ -n "${GITHUB_ENV:-}" ]; then
    [ -f "$GITHUB_ENV" ] && [ ! -L "$GITHUB_ENV" ] ||
        fail "GITHUB_ENV is not a regular file"
    printf 'CI_POSTGRES_URL=%s\nPOSTGRES_TEST_URL=%s\n' "$url" "$url" >> "$GITHUB_ENV"
fi
ready=1
trap - EXIT
log "ready: PostgreSQL 16 on 127.0.0.1:$port, database $DB"
