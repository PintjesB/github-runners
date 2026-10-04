# Oportunist runner

Lean persistent self-hosted GitHub Actions runner for `PintjesB/oportunist`.

## Included capabilities

- GitHub Actions runner
- Docker CLI, Compose v2, and Buildx
- Git and GitHub CLI
- Python 3.12 from Ubuntu 24.04
- Node.js 24
- OpenAI Codex CLI

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

## Codex authentication

Codex authentication is intentionally stored in its own Docker volume, `oportunist-runner-codex`, mounted at `/home/runner/.codex`.

Authenticate once after deployment:

```bash
docker compose exec --user runner runner codex login --device-auth
docker compose exec --user runner runner codex login status
```

Normal container recreation and image upgrades keep the login. If `oportunist-runner-codex` is deleted, simply repeat the device login. The volume does not need to be backed up.

## Workflow label

Oportunist workflows should target:

```yaml
runs-on: [self-hosted, linux, oportunist-ci]
```

The additional `codex` label allows Codex-specific jobs to select this runner explicitly if desired.

## CI evidence client

Version 1.1.0 installs `titan-evidence`, identical to the Titan client. Optional
`TITAN_EVIDENCE_BASE_URL` and `TITAN_EVIDENCE_AUDIENCE` settings carry no credentials.
See the [storage runbook](../titan/evidence/README.md); server policy denies this
repository until its consuming workflows are explicitly configured.
