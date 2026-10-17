#!/usr/bin/env bash
# Bounded GitHub Actions post-job cleanup for disposable Titan Playwright projects.
#
# The Playwright invocation normally performs docker compose down -v with its
# own Compose overlays and temporary env file. On a cancelled/killed job, this
# hook must recover without that env file, working directory, or checkout.
#
# Find containers, named volumes, and networks by Docker's *resource labels*.
# Never discover volume labels from containers and never issue a global prune.
# External runner-state/work/cache/browser volumes have unrelated project
# labels and are not eligible for deletion.
set -uo pipefail

log() { printf '[post-job] %s\n' "$*"; }

titan_project_re='^titan-stocks-playwright-'
compose_project_label='com.docker.compose.project'

remove_titan_compose_projects() {
    local projects project container volume network containers volumes networks
    # Query all three kinds of resources. A killed process may already have
    # removed its containers, leaving only project volumes or networks.
    projects="$(
        {
            docker ps -a --filter "label=$compose_project_label" \
                --format '{{.Label "com.docker.compose.project"}}' || true
            docker volume ls --filter "label=$compose_project_label" \
                --format '{{.Label "com.docker.compose.project"}}' || true
            docker network ls --filter "label=$compose_project_label" \
                --format '{{.Label "com.docker.compose.project"}}' || true
        } 2>/dev/null | grep -E "$titan_project_re" | sort -u || true
    )"
    if [ -z "$projects" ]; then
        log "no Titan Playwright resources to clean"
        return 0
    fi

    while IFS= read -r project; do
        [ -n "$project" ] || continue
        log "cleaning disposable Titan Playwright project: $project"

        # Exact project-label filtering prevents touching titan-runner,
        # non-CI Titan resources, or unrelated Docker workloads.
        containers="$(docker ps -aq \
            --filter "label=$compose_project_label=$project" 2>/dev/null || true)"
        while IFS= read -r container; do
            [ -n "$container" ] || continue
            # -v removes only anonymous volumes attached to this container.
            docker rm -fv "$container" >/dev/null 2>&1 \
                || log "could not remove project container $container (ignored)"
        done <<< "$containers"

        # Compose sets project and compose.volume labels on *volume resources*,
        # not containers. Limit removal to project-prefixed, Compose-owned
        # named volumes. Docker refuses removal of an in-use volume.
        volumes="$(docker volume ls -q \
            --filter "label=$compose_project_label=$project" \
            --filter "label=com.docker.compose.volume" 2>/dev/null || true)"
        while IFS= read -r volume; do
            [ -n "$volume" ] || continue
            case "$volume" in
                "$project"_*) ;;
                *) log "refusing non-project volume $volume"; continue ;;
            esac
            docker volume rm "$volume" >/dev/null 2>&1 \
                || log "could not remove project volume $volume (ignored)"
        done <<< "$volumes"

        networks="$(docker network ls \
            --filter "label=$compose_project_label=$project" \
            --filter "label=com.docker.compose.network" \
            --format '{{.Name}}' 2>/dev/null || true)"
        while IFS= read -r network; do
            [ -n "$network" ] || continue
            case "$network" in
                "$project"_*) ;;
                *) log "refusing non-project network $network"; continue ;;
            esac
            docker network rm "$network" >/dev/null 2>&1 \
                || log "could not remove project network $network (ignored)"
        done <<< "$networks"
    done <<< "$projects"
}

log "running Titan runner post-job cleanup"
remove_titan_compose_projects
log "post-job cleanup complete"
exit 0
