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
# The started hook fails a job when cleanup cannot be completed safely;
# completion and startup stay best effort. No host Docker object is removed.
set +e
set -u

phase="${1:-completed}"
case "$phase" in started|completed|startup) ;; *) phase=completed ;; esac

log() { printf '[runner-cleanup:%s] %s\n' "$phase" "$*"; }

dry_run="${RUNNER_CLEANUP_DRY_RUN:-0}"
diag_days="${RUNNER_CLEANUP_DIAG_DAYS:-3}"
tmp_minutes="${RUNNER_CLEANUP_TMP_MINUTES:-15}"
# Disposable package caches: clear after each job, regardless of size.
cache_max_mb="${RUNNER_CLEANUP_CACHE_MAX_MB:-0}"
# Avoid retaining many gigabytes of downloaded setup-* tool versions.
toolcache_max_mb="${RUNNER_CLEANUP_TOOLCACHE_MAX_MB:-1024}"
min_free_gb="${RUNNER_CLEANUP_MIN_FREE_GB:-15}"
min_free_inodes_pct="${RUNNER_CLEANUP_MIN_FREE_INODES_PCT:-5}"
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
        [ "$phase" = started ] && failures=$((failures + 1))
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
                    [ "$phase" = started ] && failures=$((failures + 1))
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

# An action may use $HOME/.cache or $HOME/.npm rather than the configured
# package-cache volume. These stores are disposable; never touch .config,
# .credentials, .runner, .ssh, .codex or any other runtime state.
clean_home_caches() {
    local d
    case "$runtime_dir" in
        /var/lib/general-runner/runtime) ;;
        /tmp/*) under_tmp "$runtime_dir" || return 0 ;;
        *) log "runtime cache: unsafe runtime root $runtime_dir, skipped"; return 0 ;;
    esac
    for d in "$runtime_dir/.cache" "$runtime_dir/.npm" "$runtime_dir/.local/share/pnpm/store"; do
        if [ -d "$d" ] || [ -L "$d" ]; then
            # Reject symlinks anywhere in the path and mounted directories.
            # A job must not redirect cache cleanup into protected state.
            if safe_dir "$d" && [ "$(realpath -e -- "$d" 2>/dev/null)" = "$d" ] &&
               ! mountpoint -q -- "$d" 2>/dev/null && ! has_nested_mount "$d"; then
                log "clearing disposable runtime cache $d"
                clear_contents "$d"
            else
                log "WARNING: unsafe or mounted runtime cache $d, skipped"
                [ "$phase" = started ] && failures=$((failures + 1))
            fi
        fi
    done
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
    safe_dir "$d" || {
        log "WARNING: $label: $d missing or not ours, skipped"
        if [ "$phase" = started ] && { [ "$label" = "pip cache" ] || [ "$label" = "npm cache" ]; }; then
            failures=$((failures + 1))
        fi
        return 0
    }
    is_uint "$max" || {
        log "WARNING: $label: invalid cap '$max', skipped"
        [ "$phase" = started ] && failures=$((failures + 1))
        return 0
    }
    # A failed du must not be interpreted as a zero-sized cache.
    local raw_size
    if ! raw_size="$(du -sm -x -- "$d" 2>/dev/null)"; then
        log "WARNING: $label: unable to measure $d, skipped"
        [ "$phase" = started ] && failures=$((failures + 1))
        return 0
    fi
    size="${raw_size%%
        log "$label: ${size} MB > cap ${max} MB, emptying $d"
        clear_contents "$d"
    else
        log "$label: ${size:-0} MB (cap ${max} MB), kept"
    fi
}

# Before a new job starts, reclaim what we own and refuse to run if the
# shared Docker root filesystem is too full. GitHub marks this job failed,
# but the persistent runner listener and registration remain unchanged.
# A hook is not an in-job disk quota; host-side monitoring is still needed.
prejob_storage_gate() {
    local root="$1" free_kb inode_total inode_free inode_free_pct
    is_uint "$min_free_gb" && [ "$min_free_gb" -le 1024 ] || {
        log "ERROR: invalid RUNNER_CLEANUP_MIN_FREE_GB"; return 75;
    }
    is_uint "$min_free_inodes_pct" && [ "$min_free_inodes_pct" -le 100 ] || {
        log "ERROR: invalid RUNNER_CLEANUP_MIN_FREE_INODES_PCT"; return 75;
    }
    free_kb="$(df -Pk -- "$root" 2>/dev/null | awk 'NR==2 {print $4}')"
    is_uint "$free_kb" || {
        log "ERROR: cannot read free disk space for $root"; return 75;
    }
    log "pre-job storage: $((free_kb / 1024)) MB free (minimum ${min_free_gb} GiB)"
    if [ "$free_kb" -lt "$((min_free_gb * 1024 * 1024))" ]; then
        log "ERROR: insufficient disk space; refusing to start this job"; return 75;
    fi
    read -r inode_total inode_free < <(df -Pi -- "$root" 2>/dev/null | awk 'NR==2 {print $2, $4}')
    is_uint "${inode_total:-}" && is_uint "${inode_free:-}" || {
        log "ERROR: cannot read free inodes"; return 75;
    }
    if [ "$inode_total" -gt 0 ]; then
        inode_free_pct="$((inode_free * 100 / inode_total))"
        log "pre-job inodes: ${inode_free_pct}% free (minimum ${min_free_inodes_pct}%)"
        if [ "$inode_free_pct" -lt "$min_free_inodes_pct" ]; then
            log "ERROR: insufficient free inodes; refusing to start this job"; return 75;
        fi
    fi
    return 0
}

main() {
    if [ "${RUNNER_CLEANUP_DISABLE:-0}" = 1 ]; then
        log "disabled by RUNNER_CLEANUP_DISABLE=1"
        return 0
    fi

    # RUNNER_WORK_DIR is exported by the image entrypoint, so cleanup works
    # on boot and in hooks even if RUNNER_WORKSPACE is absent in a job hook.
    local ws="${RUNNER_WORKSPACE:-}" root="${RUNNER_WORK_DIR:-}"
    if [ -z "$root" ]; then
        case "$ws" in /*) root="${ws%/*}" ;; esac
    fi
    if ! safe_dir "$root" || ! is_work_root "$root"; then
        log "ERROR: unsafe or missing work root '$root'; nothing cleaned"
        [ "$phase" = started ] && return 75
        return 0
    fi
    if [ -n "${GITHUB_WORKSPACE:-}" ]; then
        case "$GITHUB_WORKSPACE" in "$root"/*) ;; *)
            log "ERROR: GITHUB_WORKSPACE is outside $root, nothing cleaned"
            [ "$phase" = started ] && return 75
            return 0 ;;
        esac
    fi

    local before after tool
    before="$(avail_mb "$root")"
    log "work root $root: ${before:-?} MB free before cleanup$( [ "$dry_run" = 1 ] && printf ' (dry run)')"
    log "sizes before: work root $(size_mb "$root") MB, _diag $(size_mb "$diag_dir") MB, tool cache $(size_mb "${RUNNER_TOOL_CACHE:-$root/_tool}") MB"

    clean_workspaces "$root"
    clean_diag "$diag_dir"
    clean_tmp "$tmp_dir"
    clean_home_caches
    # Always target the known image cache directories. A workflow-provided
    # cache env override cannot hide stale data in the persistent volume.
    local pip_dir="${PIP_CACHE_DIR:-/var/lib/general-runner/cache/pip}"
    local npm_dir="${npm_config_cache:-${NPM_CONFIG_CACHE:-/var/lib/general-runner/cache/npm}}"
    is_cache_dir "pip cache" "$pip_dir" || pip_dir=/var/lib/general-runner/cache/pip
    is_cache_dir "npm cache" "$npm_dir" || npm_dir=/var/lib/general-runner/cache/npm
    cap_dir "pip cache" "$pip_dir" "$cache_max_mb"
    cap_dir "npm cache" "$npm_dir" "$cache_max_mb"
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
    if [ "$phase" = started ]; then
        if [ "$failures" -gt 0 ]; then
            log "ERROR: cleanup left ${failures} unremovable paths; refusing new job"
            return 75
        fi
        prejob_storage_gate "$root" || return 75
    fi
    return 0
}

main
exit $?
\t'*}"
    if ! is_uint "$size"; then
        log "WARNING: $label: invalid size for $d, skipped"
        [ "$phase" = started ] && failures=$((failures + 1))
        return 0
    fi
    if [ "$size" -gt "$max" ]; then
        log "$label: ${size} MB > cap ${max} MB, emptying $d"
        clear_contents "$d"
    else
        log "$label: ${size:-0} MB (cap ${max} MB), kept"
    fi
}

# Before a new job starts, reclaim what we own and refuse to run if the
# shared Docker root filesystem is too full. GitHub marks this job failed,
# but the persistent runner listener and registration remain unchanged.
# A hook is not an in-job disk quota; host-side monitoring is still needed.
prejob_storage_gate() {
    local root="$1" free_kb inode_total inode_free inode_free_pct
    is_uint "$min_free_gb" && [ "$min_free_gb" -le 1024 ] || {
        log "ERROR: invalid RUNNER_CLEANUP_MIN_FREE_GB"; return 75;
    }
    is_uint "$min_free_inodes_pct" && [ "$min_free_inodes_pct" -le 100 ] || {
        log "ERROR: invalid RUNNER_CLEANUP_MIN_FREE_INODES_PCT"; return 75;
    }
    free_kb="$(df -Pk -- "$root" 2>/dev/null | awk 'NR==2 {print $4}')"
    is_uint "$free_kb" || {
        log "ERROR: cannot read free disk space for $root"; return 75;
    }
    log "pre-job storage: $((free_kb / 1024)) MB free (minimum ${min_free_gb} GiB)"
    if [ "$free_kb" -lt "$((min_free_gb * 1024 * 1024))" ]; then
        log "ERROR: insufficient disk space; refusing to start this job"; return 75;
    fi
    read -r inode_total inode_free < <(df -Pi -- "$root" 2>/dev/null | awk 'NR==2 {print $2, $4}')
    is_uint "${inode_total:-}" && is_uint "${inode_free:-}" || {
        log "ERROR: cannot read free inodes"; return 75;
    }
    if [ "$inode_total" -gt 0 ]; then
        inode_free_pct="$((inode_free * 100 / inode_total))"
        log "pre-job inodes: ${inode_free_pct}% free (minimum ${min_free_inodes_pct}%)"
        if [ "$inode_free_pct" -lt "$min_free_inodes_pct" ]; then
            log "ERROR: insufficient free inodes; refusing to start this job"; return 75;
        fi
    fi
    return 0
}

main() {
    if [ "${RUNNER_CLEANUP_DISABLE:-0}" = 1 ]; then
        log "disabled by RUNNER_CLEANUP_DISABLE=1"
        return 0
    fi

    # RUNNER_WORK_DIR is exported by the image entrypoint, so cleanup works
    # on boot and in hooks even if RUNNER_WORKSPACE is absent in a job hook.
    local ws="${RUNNER_WORKSPACE:-}" root="${RUNNER_WORK_DIR:-}"
    if [ -z "$root" ]; then
        case "$ws" in /*) root="${ws%/*}" ;; esac
    fi
    if ! safe_dir "$root" || ! is_work_root "$root"; then
        log "ERROR: unsafe or missing work root '$root'; nothing cleaned"
        [ "$phase" = started ] && return 75
        return 0
    fi
    if [ -n "${GITHUB_WORKSPACE:-}" ]; then
        case "$GITHUB_WORKSPACE" in "$root"/*) ;; *)
            log "ERROR: GITHUB_WORKSPACE is outside $root, nothing cleaned"
            [ "$phase" = started ] && return 75
            return 0 ;;
        esac
    fi

    local before after tool
    before="$(avail_mb "$root")"
    log "work root $root: ${before:-?} MB free before cleanup$( [ "$dry_run" = 1 ] && printf ' (dry run)')"
    log "sizes before: work root $(size_mb "$root") MB, _diag $(size_mb "$diag_dir") MB, tool cache $(size_mb "${RUNNER_TOOL_CACHE:-$root/_tool}") MB"

    clean_workspaces "$root"
    clean_diag "$diag_dir"
    clean_tmp "$tmp_dir"
    clean_home_caches
    # Always target the known image cache directories. A workflow-provided
    # cache env override cannot hide stale data in the persistent volume.
    local pip_dir="${PIP_CACHE_DIR:-/var/lib/general-runner/cache/pip}"
    local npm_dir="${npm_config_cache:-${NPM_CONFIG_CACHE:-/var/lib/general-runner/cache/npm}}"
    is_cache_dir "pip cache" "$pip_dir" || pip_dir=/var/lib/general-runner/cache/pip
    is_cache_dir "npm cache" "$npm_dir" || npm_dir=/var/lib/general-runner/cache/npm
    cap_dir "pip cache" "$pip_dir" "$cache_max_mb"
    cap_dir "npm cache" "$npm_dir" "$cache_max_mb"
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
    if [ "$phase" = started ]; then
        if [ "$failures" -gt 0 ]; then
            log "ERROR: cleanup left ${failures} unremovable paths; refusing new job"
            return 75
        fi
        prejob_storage_gate "$root" || return 75
    fi
    return 0
}

main
exit $?
