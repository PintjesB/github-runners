# Self-hosted GitHub runners

Central repository for the self-hosted GitHub Actions runner images operated by PintjesB.

The repository is organized by runner profile. Each profile owns its image, Compose deployment, lifecycle scripts, tests, and documentation so repositories use only the capabilities they actually need.

## Runner profiles

| Profile | Purpose | Image |
| --- | --- | --- |
| [`titan`](runners/titan/) | Titan Stocks CI, including Docker tooling, Python, PostgreSQL client and Playwright/Chromium | `ghcr.io/pintjesb/titan-stocks-runner` |
| [`oportunist`](runners/oportunist/) | Oportunist CI with a lean Python/Docker toolchain | `ghcr.io/pintjesb/oportunist-runner` |
| [`general`](runners/general/) | Lightweight Ubuntu-based CI with Python, Node, Docker CLI and Compose | `ghcr.io/pintjesb/general-runner` |

Additional profiles should be added under `runners/<name>/` rather than expanding every image with unrelated dependencies.

## Repository layout

```text
runners/
  titan/
    VERSION
    Dockerfile
    docker-compose.yml
    deploy.sh
    ci-contract.sh
    scripts/
    tests/
    docs/
  general/
    VERSION
    Dockerfile
    docker-compose.yml
    ci-contract.sh
    scripts/
  oportunist/
    VERSION
    Dockerfile
    docker-compose.yml
    ci-contract.sh
    scripts/
.github/workflows/
  _publish-runner.yml
  _tag-semver.yml
  publish-titan.yml
  publish-oportunist.yml
  validate.yml
docs/
  compose-migration.md
```

## CI and publication

Pull requests that touch runner profiles or workflow files run the profile contracts before merge. Publication uses one reusable hardened workflow for both images:

1. Run the profile contract.
2. Build native `linux/amd64` and `linux/arm64` candidates.
3. Probe each candidate on a matching native hosted runner.
4. Pass exact registry digests between jobs through workflow artifacts.
5. Merge the exact candidate digests and validate the merged platform set.
6. Probe the merged manifest on both native architectures.
7. Attach provenance to the immutable merged digest.
8. Promote that exact digest to `:latest` and verify the registry-served digest matches.
9. Add an independent SemVer tag for the runner profile.

The reusable publisher deliberately tolerates the known Buildx case where `imagetools create` can return non-zero after the registry accepted the manifest. The remote merged-manifest postcondition remains authoritative.

### Profile versioning

Each profile has its own `VERSION` file. It contains the minimum SemVer release for that line, initially `1.0.0`.

After a successful publish, `_tag-semver.yml` reads all existing GHCR version tags for that image and selects the next patch release on the configured major/minor line. Examples:

```text
runners/titan/VERSION = 1.0.0
existing Titan tags    = 1.0.0, 1.0.1
next Titan release     = 1.0.2

runners/oportunist/VERSION = 1.0.0
existing Oportunist tags   = 1.0.0
next Oportunist release    = 1.0.1
```

The two profiles therefore version independently. Re-running the same already-tagged image is idempotent and reuses its existing SemVer instead of consuming another patch. To begin a new major or minor line, update that profile's `VERSION` file, for example from `1.0.0` to `1.1.0` or `2.0.0`.

Deployment examples use explicit SemVer tags so Renovate can update them. `latest` is also published for manual/rolling use.

## Design rules

- Runner profiles are independently buildable, versioned, and deployable.
- Profile-specific dependencies stay inside that profile.
- Persistent runner identity and workspaces use Docker named volumes.
- Build, verification, merge, and attestation operate on immutable digests even though deployments may consume SemVer tags.
- The dedicated CI VM is the security boundary for runners that receive the host Docker socket.

## Deployment migration

Existing runner containers do not stop because this source repository was reorganized or renamed. However, update deployment Compose files before refreshing image references. See [Compose migration after the runner repository split](docs/compose-migration.md).

The repository has already been renamed to `PintjesB/github-runners`. GitHub redirects the old repository URL, while the GHCR image names intentionally remain unchanged.
