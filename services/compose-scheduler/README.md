# Compose scheduler image

Dedicated, single-purpose scheduler for the existing GitHub Actions
Renovate merger in [PintjesB/docker-compose](https://github.com/PintjesB/docker-compose).
Source and image publishing belong here; the future deployment of two
sidecars belongs in the `docker-compose` repository.

**Image:** `ghcr.io/pintjesb/compose-scheduler`; deploy by immutable digest.

## Minimal runtime

- One statically linked Go binary on `scratch`, with CA certificates and
  embedded `Europe/Brussels` timezone data.
- No shell, package manager, listening ports, Docker socket, or database.
- Non-root UID/GID `65532:65532`; one read-only GitHub credential file.
- JSON logs and built-in liveness check; no telemetry server.
- Runtime filesystem may be read-only, with writable `/tmp` tmpfs.

## Scheduling contract

| Role | Behavior |
| --- | --- |
| `primary` | Check at 03:02:05, 03:07:05 ... 04:57:05 Brussels |
| `standby` | At primary slot +75 seconds, check GitHub; dispatch if absent |

Both roles independently calculate the same RFC3339 slot ID. The
primary checks GitHub before POST, including after container restarts.
The standby detects the exact matching workflow run by its
`display_title` (`compose-scheduler <RFC3339 slot>`), not merely by
checking that some workflow ran recently.

A GitHub `workflow_dispatch` request does **not** have an atomic
idempotency key. A run can be accepted but briefly invisible in GitHub's
listing API, so rare duplicates remain possible. The downstream
coordinator must retain its serial concurrency group and independently
check time, CI, PR identity, stale SHAs and changed-file conflicts.

The scheduler never merges PRs or deploys stacks. On a failed GET it
fails closed. After an ambiguous POST failure it does not retry the
same slot blindly. The standby and subsequent slots provide recovery.
No missed executions are replayed outside 03:00–04:59 Brussels.

## Deployment prerequisites

**Do not start the sidecars until the existing merge workflow has been
updated** to accept the following `workflow_dispatch` inputs:

```yaml
workflow_dispatch:
  inputs:
    scheduler_dispatch:
      type: boolean
      default: false
    schedule_slot:
      type: string
      default: ""
    dry_run:
      type: boolean
      default: true
```

Set its run name to exactly `compose-scheduler <schedule_slot>` for
scheduler dispatches, for example:

```yaml
run-name: ${{ inputs.scheduler_dispatch && format('compose-scheduler {0}', inputs.schedule_slot) || 'Merge independent Renovate PRs overnight' }}
```

**Critical:** `MANUAL_RUN` must exclude scheduler dispatches, so
`scheduler_dispatch=true` never grants `--manual-window-override`:

```yaml
MANUAL_RUN: ${{ github.event_name == 'workflow_dispatch' && !inputs.scheduler_dispatch && 'true' || 'false' }}
```

Remove GitHub's unreliable native cron only after proven in-window
end-to-end dispatch, successful merging, and CI-gated Komodo execution.

## Container contract

Set `SCHEDULER_ROLE=primary` at home and `SCHEDULER_ROLE=standby`
at the VPS. Each instance needs its **own** fine-grained PAT scoped to
only `PintjesB/docker-compose` with repository `Actions: Read and write`.
Mount the token only into its scheduler container as
`/run/secrets/github_dispatch_token` (optionally use `GITHUB_TOKEN_FILE`
to override the *file path*).

Do not share the PAT with either GitHub Actions runner container, or
expose it via environment variables. Mount a writable `/tmp` tmpfs
to support the built-in heartbeat healthcheck. Set `read_only: true`,
`cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`.
No ports, privileged mode, external volumes or Docker socket are needed.
Confirm secret-file read permissions for UID 65532 in each Compose stack.

Before enabling the overnight scheduler, run `docker exec <container> /scheduler --probe` to verify credential access and the GitHub API. The probe performs only a read-only list operation and cannot dispatch or merge anything.

The token is re-read from disk per API request, so rotating the file
does not require an image rebuild. On startup the token file must exist.

## Build and validation

```bash
go test -race ./...
go vet ./...
docker buildx build --platform linux/amd64,linux/arm64 .
```

On `main`, the image publisher generates `:sha-<commit>` and
`:latest` tags with multiarch manifests, SBOM and provenance. For
production, pin `ghcr.io/pintjesb/compose-scheduler@sha256:<digest>`.
The image is **not deployed** by this source repository.
