#!/usr/bin/env bash
# Register the persistent Titan Stocks runner against GitHub.
#
# Architecture
# ============
#
# Registration is the one phase that may require a fresh registration
# token. It runs inside the single ``titan-runner`` Compose container
# before the persistent listener starts. The phase:
#
#   1. Serializes concurrent registrations through an exclusive flock
#      inside the persistent state volume so two ``up`` invocations
#      cannot race the registration against an in-flight credential
#      replacement.
#   2. Inspects the existing state volume and the
#      ``diagnostics.txt`` summary to detect whether the persisted
#      identity (repository URL, runner name, label list) already
#      matches the requested identity. A complete match exits
#      successfully without contacting GitHub and without requiring a
#      token.
#   3. With a non-empty ``RUNNER_TOKEN``, registers the runner
#      against GitHub and replaces any persisted credentials with
#      the fresh ones. The previous credentials are backed up first
#      and restored on *ordinary* commit errors (the script
#      ``config.sh --replace`` exit code is documented as the only
#      commit-error surface we can roll back locally). A
#      transaction failure on the GitHub side that follows a
#      successful local commit is the documented ``--replace``
#      limitation: the remote runner record is gone and the new
#      credentials may not yet be valid; the operator must inspect
#      the failed commit manually.
#   4. Without ``RUNNER_TOKEN`` and with a missing or drifted
#      identity, fails with actionable guidance. The script does NOT
#      mutate the persistent state in this case: no backup is
#      written and the previous credentials remain untouched.
#   5. Reads ``RUNNER_TOKEN`` from the in-container environment
#      (forwarded by Compose from ``TITAN_RUNNER_TOKEN`` in ``.env``)
#      and unsets it immediately after ``config.sh`` returns so it
#      never reaches the persistent state, the diagnostics file, or
#      any later ``start-runner`` invocation.
#
# Rollback scope
# ==============
#
# The local rollback is best-effort. ``config.sh --replace`` is the
# documented one-shot registration primitive; the script does not
# claim that a GitHub-side rollback can be performed transactionally
# once the local ``config.sh --replace`` commit has succeeded. An
# ordinary commit error (a non-zero ``config.sh`` exit) restores the
# previous local credentials; the operator still has to clean up
# the GitHub-side runner record manually if the local commit
# succeeded but the GitHub handshake failed afterwards. The script
# exits non-zero on both paths so the Compose listener stays down.
#
# Environment variables
# =====================
#
#   RUNNER_TOKEN         Short-lived registration token. Optional on
#                        the steady-state path: an empty token is
#                        accepted when the persisted identity is
#                        already complete. Required when the persisted
#                        identity is missing or has drifted.
#                        Provided through the in-container environment
#                        by the sole Compose runner service during
#                        its startup registration phase.
#   REPO_URL             Repository clone URL the runner targets
#                        (e.g. ``https://github.com/owner/repo``).
#   RUNNER_NAME          Display name. Defaults to
#                        ``titan-ci-<hostname>``.
#   RUNNER_LABELS        Comma-separated custom capability labels.
#                        Defaults to ``titan-ci``. GitHub
#                        automatically attaches ``self-hosted``,
#                        ``linux``, and the architecture label
#                        (``X64`` or ``ARM64``) based on the
#                        listener's actual platform; the
#                        custom-label list intentionally omits them
#                        so a future architecture migration is a
#                        GitHub-side change rather than a
#                        ``TITAN_RUNNER_LABELS`` rotation.
#   RUNNER_STATE_DIR     Persistent state directory. Defaults to
#                        ``/var/lib/titan-runner/state``.
#   RUNNER_RUNTIME_DIR   Disposable runtime tree used during
#                        registration. Defaults to
#                        ``/var/lib/titan-runner/runtime``.
#   RUNNER_WORK_DIR      GitHub ``_work`` directory. Defaults to
#                        ``/var/lib/titan-runner/work``.
#   RUNNER_BROWSER_DIR   Playwright cache directory. Defaults to
#                        ``/var/lib/titan-runner/browser``.
#   RUNNER_ROOT          Image-owned runner tree. Defaults to
#                        ``/opt/actions-runner``.
#
# Exit codes
# ==========
#
#   0  Registration complete (or already complete).
#   1  Required configuration missing or invalid.
#   2  Registration token missing or identity drift detected.
#   3  Runner configuration failed.
#   4  Registration lock could not be acquired.
#   5  Local credential publication failed; the EXIT handler restores
#      the previous local state or clears a fresh partial state.
set -euo pipefail

log() { printf '[register] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit "${2:-1}"; }

if [ "$(id -u)" -ne 0 ]; then
    fail "register must run as root (the entrypoint sets up the runner user)" 1
fi

: "${REPO_URL:?REPO_URL is required (e.g. https://github.com/owner/repo)}"

RUNNER_LABELS="${RUNNER_LABELS:-titan-ci}"
RUNNER_NAME="${RUNNER_NAME:-titan-ci-$(hostname)}"
RUNNER_STATE_DIR="${RUNNER_STATE_DIR:-/var/lib/titan-runner/state}"
RUNNER_RUNTIME_DIR="${RUNNER_RUNTIME_DIR:-/var/lib/titan-runner/runtime}"
RUNNER_WORK_DIR="${RUNNER_WORK_DIR:-/var/lib/titan-runner/work}"
RUNNER_BROWSER_DIR="${RUNNER_BROWSER_DIR:-/var/lib/titan-runner/browser}"
RUNNER_ROOT="${RUNNER_ROOT:-/opt/actions-runner}"

log "runner_name=$RUNNER_NAME labels=$RUNNER_LABELS"
log "state_dir=$RUNNER_STATE_DIR runtime_dir=$RUNNER_RUNTIME_DIR work_dir=$RUNNER_WORK_DIR"

# Cover early validation failures before the full rollback handler is
# installed below.
trap 'unset RUNNER_TOKEN || true' EXIT

# Verify the image-owned runner binaries are present.
if [ ! -x "$RUNNER_ROOT/config.sh" ]; then
    fail "runner binaries missing from $RUNNER_ROOT; rebuild the image" 1
fi

# Ensure the persistent directories exist with the documented
# ownership and permissions before touching anything.
install -d -m 0750 -o runner -g runner \
    "$RUNNER_STATE_DIR" \
    "$RUNNER_RUNTIME_DIR" \
    "$RUNNER_WORK_DIR" \
    "$RUNNER_BROWSER_DIR"

# Serialize concurrent registration attempts through an exclusive
# flock inside the persistent state volume. The lock file lives in a
# dedicated ``.lock`` subdirectory owned by the runner user; the flock
# is released automatically when this script exits.
LOCK_DIR="$RUNNER_STATE_DIR/.lock"
LOCK_FILE="$LOCK_DIR/register.lock"
install -d -m 0750 -o runner -g runner "$LOCK_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    fail "another registration is already in flight" 4
fi

# Read the persisted identity (if any) from the diagnostics summary.
# ``diagnostics.txt`` is sanitised and operator-auditable; using it as
# the source of truth avoids parsing the JSON ``.runner`` manifest
# with a third-party tool inside the container.
EXISTING_REPO=
EXISTING_NAME=
EXISTING_LABELS=
STATE_COMPLETE=0
STATE_PERSISTENT=0
if [ -s "$RUNNER_STATE_DIR/.runner" ] && [ -s "$RUNNER_STATE_DIR/.credentials" ] \
        && [ -s "$RUNNER_STATE_DIR/diagnostics.txt" ]; then
    STATE_COMPLETE=1
    while IFS='=' read -r key value; do
        case "$key" in
            repo_url) EXISTING_REPO="${value}" ;;
            runner_name) EXISTING_NAME="${value}" ;;
            runner_labels) EXISTING_LABELS="${value}" ;;
        esac
    done < "$RUNNER_STATE_DIR/diagnostics.txt"

    # Older deployments used one-shot/ephemeral registrations. GitHub
    # de-registers those after one job, while the local credentials remain
    # in the persistent state volume. Never reuse such state as a persistent
    # listener. RunnerSettings omits false booleans, so a missing Ephemeral
    # property is equivalent to false.
    if jq -e '(.Ephemeral // false) == false' "$RUNNER_STATE_DIR/.runner" >/dev/null 2>&1; then
        STATE_PERSISTENT=1
    fi
fi

identity_matches() {
    [ "$STATE_COMPLETE" -eq 1 ] \
        && [ "$STATE_PERSISTENT" -eq 1 ] \
        && [ "$EXISTING_REPO" = "$REPO_URL" ] \
        && [ "$EXISTING_NAME" = "$RUNNER_NAME" ] \
        && [ "$EXISTING_LABELS" = "$RUNNER_LABELS" ]
}

# Back up the current credentials before any destructive step so an
# ordinary ``config.sh --replace`` failure can restore the working
# persisted credentials. The backup directory lives inside the
# persistent state volume so it survives container recreation and is
# cleaned up only on success.
backup_state() {
    local backup_dir
    backup_dir="$(mktemp -d "$RUNNER_STATE_DIR/.backup.XXXXXX")"
    chmod 0750 "$backup_dir"
    chown runner:runner "$backup_dir"
    local f
    for f in .runner .credentials .credentials_rsaparams .runner_pkey diagnostics.txt; do
        if [ -f "$RUNNER_STATE_DIR/$f" ]; then
            cp -p "$RUNNER_STATE_DIR/$f" "$backup_dir/$f"
        fi
    done
    chmod 0600 "$backup_dir"/.credentials* 2>/dev/null || true
    chown -R runner:runner "$backup_dir"
    printf '%s\n' "$backup_dir"
}

# Restore the local state volume from the backup. This is the
# local best-effort rollback surface: the GitHub-side runner
# record is intentionally NOT rolled back here. Operators must
# remove any stale runner record from the GitHub UI manually if
# ``config.sh --replace`` committed locally but failed to register
# the new identity against GitHub.
restore_state() {
    local backup_dir="$1"
    [ -d "$backup_dir" ] || return 0
    local f
    # Delete newly introduced files before restoring the previous
    # generation. Otherwise a newly-created .runner_pkey or
    # credentials_rsaparams file could survive a failed replacement.
    for f in .runner .credentials .credentials_rsaparams .runner_pkey diagnostics.txt; do
        rm -f "$RUNNER_STATE_DIR/$f"
    done
    for f in .runner .credentials .credentials_rsaparams .runner_pkey diagnostics.txt; do
        if [ -f "$backup_dir/$f" ]; then
            cp -p "$backup_dir/$f" "$RUNNER_STATE_DIR/$f"
        fi
    done
    chown -R runner:runner "$RUNNER_STATE_DIR"
    chmod 0640 "$RUNNER_STATE_DIR/.runner" 2>/dev/null || true
    chmod 0600 "$RUNNER_STATE_DIR"/.credentials* 2>/dev/null || true
}

clear_managed_state() {
    local f
    for f in .runner .credentials .credentials_rsaparams .runner_pkey diagnostics.txt; do
        rm -f "$RUNNER_STATE_DIR/$f"
    done
}

# A replacement is only committed after credentials, diagnostics, ownership,
# and permissions all succeed. On any error or signal before that point, put
# the local state volume back exactly as it was (or clear a fresh/partial
# state). This does not roll back GitHub's remote runner record.
BACKUP_DIR=
DIAGNOSTICS_TMP=
COMMIT_IN_PROGRESS=0
FRESH_STATE=0
cleanup() {
    local rc=$?
    trap - EXIT HUP INT TERM
    set +e
    unset RUNNER_TOKEN || true
    [ -n "$DIAGNOSTICS_TMP" ] && rm -f "$DIAGNOSTICS_TMP"
    if [ "$COMMIT_IN_PROGRESS" -eq 1 ]; then
        if [ -n "$BACKUP_DIR" ]; then
            restore_state "$BACKUP_DIR"
        elif [ "$FRESH_STATE" -eq 1 ]; then
            clear_managed_state
        fi
    fi
    [ -n "$BACKUP_DIR" ] && rm -rf "$BACKUP_DIR"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Decide whether to proceed with a full GitHub registration. The
# idempotency contract is:
#
#   * Complete state with matching identity  -> exit 0 (no token
#                                              required, no GitHub
#                                              call).
#   * Missing or partial state               -> require a non-empty
#                                              token.
#   * Drifted identity with a fresh token    -> re-register (with
#                                              transactional local
#                                              backup). The script
#                                              does NOT claim the
#                                              GitHub-side runner
#                                              record can be
#                                              transactionally
#                                              restored once
#                                              ``config.sh --replace``
#                                              has committed.
#   * Drifted identity without a token       -> fail with actionable
#                                              guidance; the
#                                              persistent state is
#                                              left untouched.
if identity_matches; then
    log "registration already complete with matching identity; nothing to do"
    exit 0
fi

if [ "${RUNNER_TOKEN:-}" = "" ]; then
    if [ "$STATE_COMPLETE" -eq 0 ]; then
        fail "no persisted credentials found and RUNNER_TOKEN is empty. Set TITAN_RUNNER_TOKEN in the .env file and rerun 'docker compose up -d'." 2
    fi
    if [ "$STATE_PERSISTENT" -ne 1 ]; then
        fail "persisted runner registration is ephemeral/one-shot and cannot be reused as a persistent listener. Set a fresh TITAN_RUNNER_TOKEN in the .env file and rerun './deploy.sh up' once to replace it." 2
    fi
    fail "persisted credentials have a different identity (existing_repo='$EXISTING_REPO' requested_repo='$REPO_URL'). Set TITAN_RUNNER_TOKEN in the .env file and rerun 'docker compose up -d' to refresh them." 2
fi

# A fresh token is present. Back up the existing credentials (if any)
# so an ordinary ``config.sh`` commit error can restore them. The
# local rollback does NOT cover the GitHub-side runner record; the
# operator removes any stale record from the GitHub UI manually if
# ``config.sh --replace`` committed locally but failed to register
# the new identity against GitHub.
if [ "$STATE_COMPLETE" -eq 1 ]; then
    log "persisted identity drift detected; backing up existing credentials"
    BACKUP_DIR="$(backup_state)"
else
    # Missing or partial state must not survive a failed first
    # registration as a misleading future identity.
    FRESH_STATE=1
fi
COMMIT_IN_PROGRESS=1

# Rebuild the runtime tree from the image-owned source tree. Writing
# into the runtime tree (instead of running ``config.sh`` from
# ``/opt/actions-runner`` directly) keeps the image immutable.
log "materialising runtime tree at $RUNNER_RUNTIME_DIR"
rm -rf "$RUNNER_RUNTIME_DIR"
mkdir -p "$RUNNER_RUNTIME_DIR"
cp -a "$RUNNER_ROOT/." "$RUNNER_RUNTIME_DIR/"
chown -R runner:runner "$RUNNER_RUNTIME_DIR"

# Register the runner. ``config.sh --replace`` removes an existing
# GitHub registration of the same name; ``--disableupdate`` makes the
# persisted ``.runner`` refuse subsequent in-container updates.
log "registering persistent runner against $REPO_URL"
register_args=(
    --unattended
    --replace
    --disableupdate
    --url "$REPO_URL"
    --token "$RUNNER_TOKEN"
    --name "$RUNNER_NAME"
    --labels "$RUNNER_LABELS"
    --work "$RUNNER_WORK_DIR"
)
if ! env HOME="$RUNNER_RUNTIME_DIR" gosu runner \
        "$RUNNER_RUNTIME_DIR/config.sh" "${register_args[@]}"; then
    fail "runner registration failed" 3
fi

# Unset the token immediately after ``config.sh`` returns so it
# never reaches a child process, the diagnostics summary, or any
# later listener start.
unset RUNNER_TOKEN

# Verify the new credentials landed on disk before deleting the
# backup. A successful ``config.sh`` exit is necessary but not
# sufficient: the runner has historically written the credential
# files into ``$HOME``, which we just materialised at
# ``$RUNNER_RUNTIME_DIR`` above.
if [ ! -s "$RUNNER_RUNTIME_DIR/.credentials" ] \
        || [ ! -s "$RUNNER_RUNTIME_DIR/.runner" ]; then
    fail "registration did not produce credential files" 3
fi

# Copy the mutable registration files into the persistent state
# directory. ``start-runner`` overlays them onto a freshly-built
# runtime tree on every container start. If the local publication
# step fails after a partial copy the persistent state could be
# left in a half-written state; the script aborts before any
# diagnostic summary is written so a future ``register`` run
# reads the previous diagnostics.txt (or none) and refuses to
# silently overwrite the partial state.
published_ok=1
for fname in .runner .credentials .credentials_rsaparams .runner_pkey; do
    if [ -f "$RUNNER_RUNTIME_DIR/$fname" ]; then
        if ! cp "$RUNNER_RUNTIME_DIR/$fname" "$RUNNER_STATE_DIR/$fname"; then
            log "failed to publish $fname into $RUNNER_STATE_DIR"
            published_ok=0
            break
        fi
    fi
done

if [ "$published_ok" -ne 1 ]; then
    fail "failed to publish credentials into $RUNNER_STATE_DIR" 5
fi

if ! chown -R runner:runner "$RUNNER_STATE_DIR"; then
    fail "failed to set state ownership" 5
fi
if ! chmod 0640 "$RUNNER_STATE_DIR/.runner"; then
    fail "failed to set .runner permissions" 5
fi
for credential in "$RUNNER_STATE_DIR"/.credentials*; do
    [ -e "$credential" ] || continue
    if ! chmod 0600 "$credential"; then
        fail "failed to set credential permissions" 5
    fi
done

# Save a sanitised diagnostics summary alongside the credentials so
# deployment audits can confirm which repository and label set is
# registered without exposing the credentials themselves. This is
# also the source of truth for the idempotency check above. The
# summary is written only after every credential file has landed
# on disk so a partial publish leaves no fresh ``diagnostics.txt``
# behind; the next ``register`` run sees the previous identity
# and refuses to silently overwrite the partial state.
DIAGNOSTICS_TMP="$(mktemp "$RUNNER_STATE_DIR/.diagnostics.XXXXXX")"
if ! {
    printf '# titan-runner diagnostics\n'
    printf 'registered_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'repo_url=%s\n' "$REPO_URL"
    printf 'runner_name=%s\n' "$RUNNER_NAME"
    printf 'runner_labels=%s\n' "$RUNNER_LABELS"
    printf 'runner_root=%s\n' "$RUNNER_ROOT"
    printf 'state_dir=%s\n' "$RUNNER_STATE_DIR"
    printf 'runtime_dir=%s\n' "$RUNNER_RUNTIME_DIR"
    printf 'work_dir=%s\n' "$RUNNER_WORK_DIR"
    printf 'browser_dir=%s\n' "$RUNNER_BROWSER_DIR"
    printf 'runner_version=%s\n' "${RUNNER_VERSION:-2.336.0}"
} > "$DIAGNOSTICS_TMP"; then
    fail "failed to write diagnostics" 5
fi
if ! chown runner:runner "$DIAGNOSTICS_TMP" \
        || ! chmod 0640 "$DIAGNOSTICS_TMP" \
        || ! mv "$DIAGNOSTICS_TMP" "$RUNNER_STATE_DIR/diagnostics.txt"; then
    fail "failed to publish diagnostics" 5
fi
DIAGNOSTICS_TMP=

# Discard the runtime tree; the persistent state is the only thing
# the next listener start needs.
rm -rf "$RUNNER_RUNTIME_DIR"

# The local commit is complete only after credentials, permissions, and
# diagnostics are all durable. Subsequent cleanup must not roll it back.
COMMIT_IN_PROGRESS=0

# Remove the backup now that the new credentials are persisted.
if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
    BACKUP_DIR=
fi

log "registration complete; credentials persisted to $RUNNER_STATE_DIR"
