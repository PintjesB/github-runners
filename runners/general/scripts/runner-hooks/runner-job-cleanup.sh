#!/usr/bin/env bash
# Per-job cleanup for persistent general-runner containers
# (image ghcr.io/pintjesb/general-runner).
#
# Wired into the persistent runner image through its built-in job hooks,
# preserving registration, entrypoint, labels and workflows:
#   ACTIONS_RUNNER_HOOK_JOB_STARTED   -> job-started.sh   (phase "started")
#   ACTIONS_RUNNER_HOOK_JOB_COMPLETED -> job-completed.sh (phase "completed")
#
# The runner runs both hooks inside the job, as the runner user, once per
# job. One container runs one job at a time, so at either hook nothing else
# uses this container's work volume. "started" repeats the cleanup so a job
# that was killed before its "completed" hook (SIGKILL, OOM, container or
# host restart) is cleaned up before the next job uses the runner.
#
# What it removes (only below paths the runner itself reports or the image
# configures; not through symlinks; direct and nested mount points are skipped):
#   * contents of every repository workspace under the work root
#     (checkout, .venv*, node_modules, build output); the directories stay;
#   * runner _diag logs older than RUNNER_CLEANUP_DIAG_DAYS (the newest
#     Runner_*.log, written by the live listener, is always kept);
#   * entries in /tmp owned by this user and older than
#     RUNNER_CLEANUP_TMP_MINUTES (.NET debug sockets of the listener kept);
#   * the pip / npm cache only when it is larger than
#     RUNNER_CLEANUP_CACHE_MAX_MB, the tool cache only when larger than
#     RUNNER_CLEANUP_TOOLCACHE_MAX_MB.
# It never touches _actions, _temp (the runner empties that itself),
# _PipelineMapping, the runner binaries, .runner/.credentials, the state
# volume, HOME, Docker objects, or anything outside the paths above.
#
# It must never fail a job: every error is logged and the script exits 0.
set +e
set -u

phase="${1:-completed}"
case "$phase" in started|completed) ;; *) phase=completed ;; esac

log() { printf '[runner-cleanup:%s] %s\n' "$phase" "$*"; }

dry_run="${RUNNER_CLEANUP_DRY_RUN:-0}"
diag_days="${RUNNER_CLEANUP_DIAG_DAYS:-3}"
tmp_minutes="${RUNNER_CLEANUP_TMP_MINUTES:-360}"
cache_max_mb="${RUNNER_CLEANUP_CACHE_MAX_MB:-2048}"
toolcache_max_mb="${RUNNER_CLEANUP_TOOLCACHE_MAX_MB:-3072}"
tmp_dir="${RUNNER_CLEANUP_TMP_DIR:-/tmp}"
runtime_dir="${RUNNER_RUNTIME_DIR:-/var/lib/general-runner/runtime}"
diag_dir="${RUNNER_CLEANUP_DIAG_DIR:-$runtime_dir/_diag}"

failures=0
removed=0

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

# A directory we may clean: absolute, real (no symlink), not "/" and owned by
# this user.
safe_dir() {
    local d="$1"
    case "$d" in /*) ;; *) return 1 ;; esac
    [ "$d" != "/" ] || return 1
    [ -d "$d" ] && [ ! -L "$d" ] || return 1
    [ -O "$d" ] || return 1
    return 0
}

# Workflow env must not redirect cleanup into credentials, _actions, _temp,
# HOME or other persistent directories. Permit known image paths and /tmp
# fixture paths only. Canonicalize /tmp paths to reject symlinked parents.
under_tmp() {
    local resolved
    resolved="$(realpath -e -- "$1" 2>/dev/null)" || return 1
    case "$resolved" in /tmp/?*) return 0 ;; *) return 1 ;; esac
}

is_work_root() {
    case "$1" in
        /var/lib/general-runner/work|/var/lib/oportunist-runner/work) return 0 ;;
        /tmp/*) under_tmp "$1" ;;
        *) return 1 ;;
    esac
}

is_cache_dir() {
    local label="$1" d="$2"
    case "$label:$d" in
        "pip cache:/var/lib/general-runner/cache/pip"|"npm cache:/var/lib/general-runner/cache/npm") return 0 ;;
    esac
    case "$label:$d" in
        "pip cache:/tmp/"*"/cache/pip"|"npm cache:/tmp/"*"/cache/npm") under_tmp "$d" ;;
        *) return 1 ;;
    esac
}

# Catch bind mounts even when they use the same underlying filesystem.
# Refuse deletion if mount inspection fails; never chmod through a mount.
has_nested_mount() {
    local p="$1" mount_path mounts
    command -v findmnt >/dev/null 2>&1 || return 0
    mounts="$(findmnt -rn -o TARGET 2>/dev/null)" || return 0
    [ -n "$mounts" ] || return 0
    while IFS= read -r mount_path; do
        case "$mount_path" in "$p"/*) return 0 ;; esac
    done <<< "$mounts"
    return 1
}

size_mb() { du -sm -x -- "$1" 2>/dev/null | awk '{s=$1} END {print s+0}'; }
avail_mb() { df -Pm -- "$1" 2>/dev/null | awk 'NR==2 {print $4+0}'; }

# Remove one path. Symlinks are unlinked, never followed. Mount points are
# skipped. Read-only directories left by a job are made writable first.
remove_path() {
    local p="$1"
    if [ ! -L "$p" ] && { mountpoint -q -- "$p" 2>/dev/null || { [ -d "$p" ] && has_nested_mount "$p"; }; }; then
        log "skip mounted path: $p"
        return 0
    fi
    removed=$((removed + 1))
    if [ "$dry_run" = 1 ]; then
        [ "$removed" -le 25 ] && log "dry-run: would remove $p"
        return 0
    fi
    if [ ! -L "$p" ] && [ -d "$p" ]; then
        chmod -R u+rwX -- "$p" 2>/dev/null
    fi
    if ! rm -rf --one-file-system -- "$p" 2>/dev/null || [ -e "$p" ] || [ -L "$p" ]; then
        log "WARNING: could not fully remove $p (owned by another user?)"
        failures=$((failures + 1))
    fi
}

clear_contents() {
    local d="$1" entry
    local -a entries=()
    shopt -s dotglob nullglob
    entries=("$d"/*)
    shopt -u dotglob nullglob
    for entry in "${entries[@]}"; do
        remove_path "$entry"
    done
}

clean_workspaces() {
    local root="$1" repo sub
    shopt -s nullglob
    local -a repos=("$root"/*)
    shopt -u nullglob
    for repo in "${repos[@]}"; do
        case "${repo##*/}" in _*) continue ;; esac   # runner-owned: _actions, _temp, _tool, ...
        [ -d "$repo" ] && [ ! -L "$repo" ] || continue
        shopt -s nullglob dotglob
        local -a subs=("$repo"/*)
        shopt -u nullglob dotglob
        for sub in "${subs[@]}"; do
            if [ -d "$sub" ] && [ ! -L "$sub" ]; then
                # Do not traverse a mounted workspace or nested bind mount,
                # even when it shares the work volume's underlying device.
                if mountpoint -q -- "$sub" 2>/dev/null || has_nested_mount "$sub"; then
                    log "skip mounted workspace: $sub"
                    continue
                fi
                # <repo>/<repo> is the job workspace: keep the directory.
                clear_contents "$sub"
            else
                remove_path "$sub"
            fi
        done
    done
}

clean_diag() {
    local d="$1" newest
    case "$d" in
        /var/lib/general-runner/runtime/_diag) ;;
        /tmp/*/_diag) under_tmp "$d" || { log "unsafe _diag $d, skipped"; return 0; } ;;
        *) log "unsafe _diag $d, skipped"; return 0 ;;
    esac
    safe_dir "$d" || { log "no runner _diag at $d, skipped"; return 0; }
    is_uint "$diag_days" || diag_days=3
    newest="$(ls -1t -- "$d"/Runner_*.log 2>/dev/null | head -n 1)"
    while IFS= read -r -d '' f; do
        [ "$f" = "$newest" ] && continue
        remove_path "$f"
    done < <(find "$d" -xdev -type f \( -name 'Runner_*.log' -o -name 'Worker_*.log' -o -path "$d/pages/*" -o -path "$d/blocks/*" \) -mtime +"$diag_days" -print0 2>/dev/null)
}

clean_tmp() {
    local d="$1"
    case "$d" in
        /tmp) ;;
        /tmp/*/tmp) under_tmp "$d" || { log "unsafe tmp path $d, skipped"; return 0; } ;;
        *) log "unsafe tmp path $d, skipped"; return 0 ;;
    esac
    [ -d "$d" ] && [ ! -L "$d" ] || return 0
    is_uint "$tmp_minutes" || tmp_minutes=360
    while IFS= read -r -d '' f; do
        case "${f##*/}" in clr-debug-pipe-*|dotnet-diagnostic-*|.dotnet|.X11-unix|.ICE-unix) continue ;; esac
        remove_path "$f"
    done < <(find "$d" -mindepth 1 -maxdepth 1 -user "$(id -u)" -mmin +"$tmp_minutes" -print0 2>/dev/null)
}

cap_dir() {
    local label="$1" d="$2" max="$3" size
    [ -n "$d" ] || return 0
    if [ "$label" != "tool cache" ] && ! is_cache_dir "$label" "$d"; then
        log "$label: unsafe location $d, skipped"
        return 0
    fi
    safe_dir "$d" || { log "$label: $d missing or not ours, skipped"; return 0; }
    is_uint "$max" || { log "$label: invalid cap '$max', skipped"; return 0; }
    size="$(size_mb "$d")"
    if [ "${size:-0}" -gt "$max" ]; then
        log "$label: ${size} MB > cap ${max} MB, emptying $d"
        clear_contents "$d"
    else
        log "$label: ${size:-0} MB (cap ${max} MB), kept"
    fi
}

main() {
    if [ "${RUNNER_CLEANUP_DISABLE:-0}" = 1 ]; then
        log "disabled by RUNNER_CLEANUP_DISABLE=1"
        return 0
    fi

    local ws="${RUNNER_WORKSPACE:-}" root
    case "$ws" in /*) ;; *) log "RUNNER_WORKSPACE not set or not absolute, nothing cleaned"; return 0 ;; esac
    root="${ws%/*}"
    # The work root must be a real directory of ours that the runner has
    # set up (it always creates _temp there). Anything else: do nothing.
    if ! safe_dir "$root" || ! is_work_root "$root" || [ ! -d "$root/_temp" ]; then
        log "work root '$root' does not look like a runner work directory, nothing cleaned"
        return 0
    fi
    if [ -n "${GITHUB_WORKSPACE:-}" ]; then
        case "$GITHUB_WORKSPACE" in "$root"/*) ;; *)
            log "GITHUB_WORKSPACE is outside $root, nothing cleaned"; return 0 ;;
        esac
    fi

    local before after tool
    before="$(avail_mb "$root")"
    log "work root $root: ${before:-?} MB free before cleanup$( [ "$dry_run" = 1 ] && printf ' (dry run)')"
    log "sizes before: work root $(size_mb "$root") MB, _diag $(size_mb "$diag_dir") MB, tool cache $(size_mb "${RUNNER_TOOL_CACHE:-$root/_tool}") MB"

    clean_workspaces "$root"
    clean_diag "$diag_dir"
    clean_tmp "$tmp_dir"
    cap_dir "pip cache" "${PIP_CACHE_DIR:-}" "$cache_max_mb"
    cap_dir "npm cache" "${npm_config_cache:-${NPM_CONFIG_CACHE:-}}" "$cache_max_mb"
    tool="${RUNNER_TOOL_CACHE:-$root/_tool}"
    case "$tool" in "$root/_tool") cap_dir "tool cache" "$tool" "$toolcache_max_mb" ;;
        *) log "tool cache $tool is outside the work root, skipped" ;; esac

    after="$(avail_mb "$root")"
    if is_uint "${before:-}" && is_uint "${after:-}"; then
        # Free space is filesystem-wide: other containers on the host change it too.
        log "done: ${removed} path(s) $( [ "$dry_run" = 1 ] && printf 'would be ')removed, ${failures} not removable; ${after} MB free after cleanup (change $((after - before)) MB)"
    else
        log "done: ${removed} path(s) $( [ "$dry_run" = 1 ] && printf 'would be ')removed, ${failures} not removable"
    fi
    return 0
}

main || true
exit 0
