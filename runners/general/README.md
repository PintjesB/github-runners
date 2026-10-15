# General-purpose runner

Reusable Ubuntu 24.04 GitHub Actions runner for lightweight repository CI.
Includes Python 3.12, Node.js 24, Git, GitHub CLI, ShellCheck and the Docker
client with Compose and Buildx. No Codex, browser, PostgreSQL client,
Docker daemon or host Docker socket.

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
