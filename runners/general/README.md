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

The completion hook deletes repository workspace contents and aged runner-owned
temporary/diagnostic files. Pip and npm caches are emptied above 2048 MB each,
and the tool cache above 3072 MB. The started hook repeats cleanup before the
next job's checkout to recover after a job is interrupted. Registration,
credentials, _actions and _temp are never removed by these hooks.

For first deployment, optionally set RUNNER_CLEANUP_DRY_RUN=1 as a Compose
environment override while runners are idle. Verify both [runner-cleanup:started]
and [runner-cleanup:completed] in GitHub job logs. Remove the override and
redeploy while idle to activate deletion, which is the image default.
RUNNER_CLEANUP_DISABLE=1 disables cleanup if rollback is needed.

The hooks cannot reclaim Docker anonymous PostgreSQL service volumes, images,
or logs on the host. SIGKILL and host shutdown defer cleanup to the next job.
Persistent listeners remain less isolated than one-new-container-per-job;
cleanup is disk hygiene, not a security boundary. Measure disk usage over
20 jobs and verify that named volumes and runtime files remain intact.

Run bash ci-contract.sh from runners/general to validate syntax, ShellCheck,
and standard-library tests. Native image validation checks the baked scripts.
