# General-purpose runner

Reusable Ubuntu 24.04 GitHub Actions runner for lightweight repository CI.
Includes Python 3.12, Node.js 24, Git, GitHub CLI, ShellCheck, PostgreSQL
client 16 (`psql`) and the Docker client with Compose and Buildx. No Codex,
browser, Docker daemon or host Docker socket.

The image supports both amd64 and arm64 and uses a persistent GitHub runner
identity with separate state, work and package-cache volumes.

## Deployment

Set `GENERAL_RUNNER_IMAGE` to a published image tag or digest,
`GENERAL_RUNNER_REPO_URL` to the target repository, and
`GENERAL_RUNNER_INSTANCE` to a unique name per deployed listener.
Acquire a short-lived registration token in that repository's
Settings > Actions > Runners and set `GENERAL_RUNNER_TOKEN`.
Deploy `docker-compose.yml`; verify the `general-ci` label appears online.
Remove the token and recreate the container while preserving volumes.

Each repository needs its own registered listener and separate volumes.
The CLI can validate Compose files without a daemon; building or running
containers requires a deliberately configured external Docker daemon.

## Built-in per-job cleanup

The image configures the GitHub Actions runner job-started and job-completed
hooks from /opt/runner-hooks/. No external Compose mount, additional Docker
socket access, systemd service or workflow changes are necessary.

The completion hook removes repository workspace contents and aged runner-owned
temporary/diagnostic files. On container startup, the image also cleans
abandoned job data before starting the persistent listener.

Pip and npm caches are **disposable by default** and are emptied after each
job and before the next. Common disposable runtime-home caches (`~/.cache`,
`~/.npm`, and the pnpm store) are also cleared, without touching credentials,
registration or other runtime state. Runner-owned /tmp entries older than
15 minutes are removed; diagnostics older than three days are pruned.
The tool cache is emptied if it exceeds 1024 MB.
The entrypoint exports RUNNER_WORK_DIR explicitly: cleanup does not depend on
GitHub providing RUNNER_WORKSPACE.

The job-started hook first cleans and then checks free disk capacity. A job
is refused if free space is below 15 GiB or free inodes are below 5%
(RUNNER_CLEANUP_MIN_FREE_GB and RUNNER_CLEANUP_MIN_FREE_INODES_PCT).
This fails **the assigned job**, not the listener; runner identity, registration,
volumes and image remain unchanged. Cleanup failures, unmeasurable package caches and leftover mounted
workspaces also fail the new job rather than silently proceeding. Completion
and startup cleanup remain best effort: any failure is logged and retried before
another job. Hooks and startup cleanup have a five-minute process timeout
because GitHub does not impose one. These guards do not impose a quota while
a job is actively running.

Registration, credentials, _actions and _temp are never removed. Cleanup
targets are restricted to known work/cache/runtime paths and /tmp; symlinked
paths and nested bind mounts are protected. Unknown layouts are skipped.

For first deployment, optionally set RUNNER_CLEANUP_DRY_RUN=1 as a Compose
environment override while runners are idle. Verify both [runner-cleanup:started]
and [runner-cleanup:completed] in GitHub job logs. Remove the override and
redeploy while idle to activate deletion, which is the image default.
RUNNER_CLEANUP_DISABLE=1 disables cleanup if rollback is needed.

The hooks cannot reclaim Docker anonymous PostgreSQL service volumes, images,
or logs on the host. SIGKILL and host shutdown defer cleanup to the next
container start or job-start hook. No host-wide Docker prune is performed.
A single job can still exhaust the host by creating Docker service volumes,
large writable layers or build output before its completion hook runs. Keep
Checkmk disk alerts and job-scoped Docker service teardown.
Persistent listeners remain less isolated than one-new-container-per-job;
cleanup is disk hygiene, not a security boundary. Measure disk usage over
20 jobs and verify that named volumes and runtime files remain intact.

Run bash ci-contract.sh from runners/general to validate syntax, ShellCheck,
and standard-library tests. Native image validation checks the baked scripts. Before rollout, verify a real
job's `Set up runner` and `Complete runner` hook log steps on one updated
listener, then run another job on that same listener without re-registering.
Confirm `docker volume inspect`, free bytes and the runner's unchanged ID
before extending the rollout.

## On-demand PostgreSQL 16 for CI

The image includes PostgreSQL 16 server/client binaries and the
`en_US.UTF-8` locale. It does **not** run PostgreSQL by default, create a
persistent cluster, or require access to a Docker daemon.

A workflow needing PostgreSQL starts a private disposable cluster:

```yaml
# Remove that job's `services: postgres` block first.
steps:
  - uses: actions/checkout@<your-existing-pinned-sha>
  - name: Start isolated PostgreSQL 16
    run: runner-ci-postgres start
  - name: Run database tests
    run: python -m pytest tests/
```

`runner-ci-postgres start` creates a new cluster under the runner-owned work
volume (`_ci_postgres`, outside repository checkout paths), with SCRAM
password authentication, a random password and port, and TCP listening
**only** on `127.0.0.1`. It checks the actual server settings. It exports
`CI_POSTGRES_URL` and `POSTGRES_TEST_URL` for subsequent Actions steps via
`GITHUB_ENV`; no password is printed. A local shell may source the protected
`_ci_postgres/connection.env` file. The default database is `ci_test`;
set `RUNNER_CI_POSTGRES_DB` or `RUNNER_CI_POSTGRES_USER` for test compatibility.
These settings are evaluated only when `start` is called.

The image's existing lifecycle hooks now also call
`runner-ci-postgres stop` before each job and after every job, including
failed jobs. The container startup path does the same after a restart.
Stop verifies that PostgreSQL is no longer running before deleting its
cluster. Database cleanup runs even if generic cache cleanup is disabled.
The listener's persistent registration/state remains untouched. The native
image probe runs a real start, SQL query and stop on both architectures.

**Consumer migration is still required.** GitHub Actions `services:` always
uses a Docker daemon, even when PostgreSQL is available in the runner image.
Remove the service definition and its `job.services.postgres.ports` lookups
from each consuming workflow and use `POSTGRES_TEST_URL` instead. For
workflows expecting `DATABASE_URL`, map it explicitly to this job-specific
test URL. Jobs that build or run *other* Docker containers still need their
existing Docker execution environment.

Only remove `/var/run/docker.sock` from a runner's Compose service after
**all** jobs scheduled on that runner have been verified to run without
GitHub service containers, job containers, Docker builds or direct daemon
commands. Publishing this image alone does not remove that socket.
