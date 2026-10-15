# Compose migration after the runner repository split

The repository move to `runners/titan/` and `runners/oportunist/` does not by itself stop already-running containers. The deployment files on a runner VM are local files, and the existing Titan GHCR image name remains `ghcr.io/pintjesb/titan-stocks-runner`.

## Titan

An existing Titan Compose deployment can keep running while you schedule the migration. Update it before the next normal image refresh.

Use the current files from `runners/titan/` as the source of truth.

The existing `titan-runner-state`, `titan-runner-work`, and `titan-runner-browser` volume names are unchanged, so adopting the current Compose file reuses the existing GitHub registration, workspace, and browser cache.

Runner releases now publish independent SemVer tags in addition to `latest`. Prefer an explicit version in `.env`, for example:

```text
TITAN_RUNNER_IMAGE=ghcr.io/pintjesb/titan-stocks-runner:1.0.0
```

This keeps deployments predictable while allowing Renovate to propose image upgrades automatically. If the deployment uses `:latest`, run `docker compose pull` before recreating the runner; `latest` moving in GHCR does not restart a running container by itself.

## Oportunist

Use `runners/oportunist/docker-compose.yml` rather than adapting the Titan Compose file. Its persistent state is intentionally separate:

- `oportunist-runner-state`
- `oportunist-runner-work`

Use the profile's explicit SemVer image tag so Renovate can track upgrades:

```text
OPORTUNIST_RUNNER_IMAGE=ghcr.io/pintjesb/oportunist-runner:1.0.0
```

After first registration, clear `OPORTUNIST_RUNNER_TOKEN` from `.env` and recreate the container so the short-lived token is no longer present in Docker's stored container environment metadata.

## Versioning

Titan and Oportunist version independently. Each profile's `VERSION` file defines the minimum SemVer on its current major/minor line. Successful publishes inspect the existing GHCR tags and add the next patch release, while re-running an already-published digest reuses its existing SemVer tag.

To deliberately start a new release line, update only that profile's `VERSION` file, for example `1.0.0` to `1.1.0` or `2.0.0`.

## Repository rename

The GitHub repository is now `PintjesB/github-runners`. Existing Git remotes pointing at the former repository name are redirected by GitHub, but update local remotes when convenient:

```bash
git remote set-url origin git@github.com:PintjesB/github-runners.git
```

The GHCR package names intentionally remain independent of the source repository name.
