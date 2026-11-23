# Oportunist runner

For the optional `oportunist-ci-light` listener, persistent pip/npm volumes,
release ordering, and bounded cache maintenance, see the
[CI capacity runbook](../../docs/ci-capacity-and-caches.md).

Lean persistent self-hosted GitHub Actions runner for `PintjesB/oportunist`.

## Included capabilities

- GitHub Actions runner
- Docker CLI, Compose v2, and Buildx
- Git and GitHub CLI
- Python 3.12 from Ubuntu 24.04
- Node.js 24

Titan-only PostgreSQL, Playwright, Chromium, browser-cache, and cleanup tooling are intentionally omitted.

## Deploy

```bash
cp .env.example .env
chmod 0600 .env
```

Set `OPORTUNIST_RUNNER_IMAGE` to the published image, preferably by immutable digest. For first registration fetch a short-lived token:

```bash
gh api -X POST repos/PintjesB/oportunist/actions/runners/registration-token | jq -r .token
```

Put it in `OPORTUNIST_RUNNER_TOKEN`, then start the runner:

```bash
docker compose up -d
```

After registration succeeds, blank `OPORTUNIST_RUNNER_TOKEN` and recreate the service. Persistent runner identity lives in `oportunist-runner-state`.

If that volume contains an older ephemeral/one-shot registration, startup
intentionally refuses to reuse it. Set a fresh `OPORTUNIST_RUNNER_TOKEN` and
recreate the service once so GitHub replaces it with a persistent registration.

## Workflow label

Oportunist workflows should target:

```yaml
runs-on: [self-hosted, linux, oportunist-ci]
```

## CI evidence client

Version 1.1.0 installs `titan-evidence`, identical to the Titan client. Optional
`TITAN_EVIDENCE_BASE_URL` and `TITAN_EVIDENCE_AUDIENCE` settings carry no credentials.
See the [storage runbook](../titan/evidence/README.md); server policy denies this
repository until its consuming workflows are explicitly configured.

## Persistent per-job disk cleanup

This image embeds per-job cleanup without container recreation or re-registration.
The existing state, work and cache volumes remain mounted across jobs.

On each container startup, before every assigned job and on job completion, the
runner clears disposable repository checkouts and build outputs, pip/npm caches,
common runtime-home caches and old temporary files. Downloaded runner tool
caches above 1 GiB are emptied. Pre-job cleanup refuses new work if cleanup
is incomplete or filesystem free space is below 15 GiB / free inodes below 5%.
Cleanup invocations are bounded to five minutes. The browser cache, where
configured, stays persistent.

**Safety boundary:** Cleanup only touches paths owned by this runner and never
uses Docker prune or removes host containers, images, networks, BuildKit state,
or anonymous volumes. Docker cleanup belongs in each workflow's own scoped
`trap`/teardown logic, especially on hosts running multiple CI jobs.

`RUNNER_CLEANUP_DRY_RUN=1` previews deletions and
`RUNNER_CLEANUP_DISABLE=1` disables cleanup for rollback. Actual job hooks
must be verified on an idle canary after the image is published.
