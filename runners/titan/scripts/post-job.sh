#!/usr/bin/env bash
# GitHub Actions runner post-job hook for Titan Stocks.
#
# The hook runs in the runner user's context immediately after a
# workflow job finishes. It enforces a bounded hygiene contract
# anchored on the documented ``titan-stocks-playwright-`` project
# label:
#
#   * Only Compose projects whose name matches the documented
#     Titan CI prefix ``titan-stocks-playwright-`` are torn down.
#     The runner's own ``titan-runner`` container is left alone.
#   * The shared Playwright browser volume and the persistent
#     ``titan-runner-state``, ``titan-runner-work``,
#     ``titan-runner-browser`` volumes
#     are NEVER touched. ``docker compose down -v`` removes only
#     volumes owned by the matching child project; the external
#     runner volumes are excluded by construction.
#   * The shared Playwright ``.cache/ms-playwright`` and any
#     Docker build cache are NEVER pruned.
#   * No ``docker system prune``, ``docker volume prune``, ``docker
#     image prune``, ``docker builder prune``, or ``docker network
#     prune`` is invoked.
#   * The runner ``_work`` directory is NEVER recursed into. The
#     bounded cleanup only targets the matching Compose projects;
#     any non-Titan checkout, any application development volume,
#     and any unrelated workspace is left alone. The runner-owned
#     named volumes titan-runner-state, titan-runner-work,
#     titan-runner-browser are protected
#     storage and are never removed by this hook.
#
# The hook is intentionally permissive on failure: a cleanup error
# must not fail the workflow job that just succeeded. Errors are
# logged so operators can investigate, but the exit code is always
# zero unless something fundamentally unsafe happens.
set -uo pipefail

log() { printf '[post-job] %s\n' "$*"; }

# The CI prefix matches the project name the Playwright stack
# uses (``titan-stocks-playwright-<profile>-<pid>``). Any other
# ``titan-*`` resource on the runner host belongs to a different
# workload and must not be touched here. The prefix is anchored on
# the documented ``titan-stocks-playwright-`` literal so the runner
# container (``titan-runner``) and any future ``titan-`` workload
# cannot be torn down by mistake.
titan_project_re='^titan-stocks-playwright-'
compose_project_label='com.docker.compose.project'

remove_titan_compose_projects() {
    # ``docker compose ls`` is not available in every docker CLI
    # version, so we list the containers directly and recover the
    # Compose project label. ``docker compose --project-name <name>
    # down -v`` removes the project's containers, its bridge
    # network, and its project-owned volumes; external volumes are
    # left alone, so titan-runner-state, titan-runner-work,
    # titan-runner-browser, and application
    # development volumes are never affected.
    local projects
    projects="$(docker ps -a --filter label="$compose_project_label" \
        --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
        | grep -E "$titan_project_re" | sort -u || true)"
    if [ -z "$projects" ]; then
        log "no Titan CI Compose projects to tear down"
        return 0
    fi
    local project
    for project in $projects; do
        log "tearing down Titan CI Compose project: $project"
        docker compose --project-name "$project" down -v \
            >/dev/null 2>&1 || log "compose down failed for $project (ignored)"
    done
}

remove_titan_volumes() {
    # ``docker volume ls --filter label=...`` is not portable
    # across every docker CLI version, so we read the
    # ``com.docker.compose.project`` label off the container that
    # owns each anonymous volume, then match it against the CI
    # prefix. Only anonymous volumes whose owning container
    # carries the documented label are removed. Named volumes
    # (such as ``titan-runner-state``, ``titan-runner-work``,
    # ``titan-runner-browser``, and any
    # application development volume) are excluded by construction.
    local volumes
    volumes="$(docker ps -a --filter label="$compose_project_label" \
        --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.volume"}}' \
        2>/dev/null \
        | awk -F'|' '$1 ~ /'"$titan_project_re"'/ && $2 != "" {print $2}' \
        | sort -u || true)"
    if [ -z "$volumes" ]; then
        log "no Titan CI anonymous volumes to remove"
        return 0
    fi
    local volume
    for volume in $volumes; do
        case "$volume" in
            titan-runner-state|titan-runner-work|titan-runner-browser)
                log "leaving protected runner volume untouched: $volume"
                continue
                ;;
        esac
        log "removing Titan CI volume: $volume"
        docker volume rm "$volume" >/dev/null 2>&1 \
            || log "volume rm failed for $volume (ignored)"
    done
}

log "running Titan runner post-job cleanup"
remove_titan_compose_projects
remove_titan_volumes
log "post-job cleanup complete"
exit 0
