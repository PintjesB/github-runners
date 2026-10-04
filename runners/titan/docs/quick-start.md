# Quick start

This procedure covers the first-time deployment of a persistent
`PintjesB/titan-stocks` runner on a dedicated CI VM. The **VM is
the security boundary**: the runner container has unrestricted
access to the VM's Docker daemon, while the VM is isolated from
production systems and the rest of the private network. The
VM-level network isolation contract is documented in
[vm-deployment.md](vm-deployment.md); the acceptance checks in
that document MUST pass before the listener is brought up.

The image is published as a multi-platform manifest with both
native `linux/amd64` and `linux/arm64` entries; the VM can be
either architecture. The runner listener runs once on whichever
compatible `titan-ci` runner is available. `deploy.sh` maps the
host's `uname -m` output to the matching `linux/amd64` or
`linux/arm64` platform so a build, a probe, and a `docker run
--platform` invocation always resolve to a native architecture.

The VM must already run Docker Engine with the Compose v2 plugin
and Buildx on `PATH`. The VM firewall MUST follow the inbound
and outbound rules in [vm-deployment.md](vm-deployment.md)
before the first `docker compose up -d`.

## 1. Storage tiers

The runner uses four storage tiers. State, work, and browser are
explicit Docker-named volumes that Compose creates on the first
`docker compose up -d`; the Docker socket is the only host bind
mount. The work volume is always mounted at
`/var/lib/titan-runner/work` and starts empty when migrating from
the previous host-directory layout.

* **`state`** &mdash; persistent Docker-named volume. Holds the
  GitHub-issued Actions runner credentials and a sanitised
  diagnostics summary. Compose creates the `titan-runner-state`
  volume on first `up`.
* **`browser`** &mdash; persistent Docker-named volume. Holds the
  Playwright Chromium browser cache. Compose creates the
  `titan-runner-browser` volume on first `up`; `start-runner.sh`
  seeds it from the baked image cache on the first start.
* **`work`** &mdash; persistent Docker-named volume
  `titan-runner-work`, mounted at `/var/lib/titan-runner/work` and
  owned by `runner:runner`. GitHub recreates checkouts there after
  migration; the previous host workspace directory is not copied or
  deleted. Child workflow Compose services must mount the same
  external volume at the same fixed path when they need checkout
  files.
* **`runtime`** &mdash; disposable. Materialised from the image on
  every container start by `start-runner.sh`. Lives only inside
  the listener container's writable layer.

Child workflow Compose contract:

```yaml
volumes:
  titan-runner-work:
    external: true
    name: titan-runner-work

services:
  app:
    volumes:
      - titan-runner-work:/var/lib/titan-runner/work
```

Use checkout paths under `/var/lib/titan-runner/work`; host bind
paths and workspace path overrides are not supported deployment inputs.

## 2. Configure the deployment env file

`docker compose` and `deploy.sh` read the deployment variables
from a gitignored `.env` file (default: the runner repository's
`.env`). The file must be mode `0600`. Copy the committed template
and edit it:

```bash
cp .env.example .env
chmod 0600 .env
```

`docker compose` only interpolates the entries explicitly
referenced by the `docker-compose.yml` contract; arbitrary `.env`
entries do not reach the running services. The documented entries
are:

* `TITAN_RUNNER_IMAGE` &mdash; pinned image digest. The image is
  a multi-platform manifest; `docker compose pull` resolves the
  matching native platform for the VM's architecture.
* `TITAN_RUNNER_REPO_URL` &mdash; repository the runner targets.
* `TITAN_RUNNER_NAME` &mdash; display name (defaults to
  `titan-ci`).
* `TITAN_RUNNER_LABELS` &mdash; custom capability labels (defaults
  to `titan-ci`). GitHub automatically attaches `self-hosted`,
  `linux`, and `X64` / `ARM64` based on the listener's actual
  platform; the custom-label list intentionally omits them so a
  future architecture migration is a GitHub-side change rather
  than a `TITAN_RUNNER_LABELS` rotation.
* `TITAN_RUNNER_TOKEN` &mdash; short-lived registration token.
  Required only on the first `docker compose up -d` (or after a
  credential rotation); blank the line after successful
  registration and re-run `docker compose up -d`.
* Optional path overrides; defaults match the documented layout.

`deploy.sh` parses only the documented keys. Arbitrary entries
in the file are silently dropped; the file is never
shell-sourced.

## 3. Bootstrap a short-lived registration token

GitHub registration tokens are valid for about an hour. The token
is read by the runner startup entrypoint as `RUNNER_TOKEN`, unset
before the listener is launched, and never persisted in the image,
the state volume, or the listener process environment.

```bash
gh api -X POST \
  repos/PintjesB/titan-stocks/actions/runners/registration-token \
  | jq -r .token
```

Paste the token onto the `TITAN_RUNNER_TOKEN=` line in `.env`. A
SOPS-decrypted file, an AWS Secrets Manager `get-secret-value`
output, or a HashiCorp Vault read are equally valid; the value is
forwarded by Compose to the runner startup phase as `RUNNER_TOKEN`
and the shell never logs the literal value.

## 4. Pull the image

The image is published as
`ghcr.io/pintjesb/titan-stocks-runner:latest`, a multi-platform
manifest with both `linux/amd64` and `linux/arm64` entries.
Resolve the immutable digest that backs the tag, then pin by
digest:

```bash
docker buildx imagetools inspect ghcr.io/pintjesb/titan-stocks-runner:latest
docker pull ghcr.io/pintjesb/titan-stocks-runner@sha256:<digest>
```

`docker compose pull` resolves the matching native platform from
this single digest so a multi-platform manifest can be deployed
on either AMD64 or ARM64 VMs without re-tagging.

## 5. Bring up the stack

`./deploy.sh up` is the only command required. The single
runner container performs registration during startup and launches
the listener only after it succeeds:

```bash
docker compose pull
./deploy.sh up
```

The internal `register.sh` phase is idempotent:

* If the persistent `state` volume already contains matching
  `.runner`, `.credentials`, and `diagnostics.txt` (matching
  repository URL, runner name, and label list), registration exits
  successfully without contacting GitHub and without requiring
  `TITAN_RUNNER_TOKEN`.
* If the state is missing, has drifted, or its persisted `.runner`
  manifest is an old ephemeral/one-shot registration, registration contacts
  GitHub using `RUNNER_TOKEN` (forwarded from
  `TITAN_RUNNER_TOKEN`) and persists the new credentials into
  the same volume. Ephemeral state is never reused because GitHub de-registers
  one-shot runners after their first job. A transactional local backup ensures that an
  ordinary `config.sh` commit error restores the previously
  working credentials. The local rollback is best-effort; the
  GitHub-side runner record is not transactionally restored
  after `config.sh --replace` and must be cleaned up manually
  if a partial commit leaves a stale remote entry.
* A missing `RUNNER_TOKEN` together with a missing or drifted
  state fails startup with actionable guidance so the listener
  cannot come up with broken credentials.

The listener uses the image's default entrypoint
(`tini` -> `start-runner`). The token is *never* mounted on the
long-running listener; the runner authenticates with the
GitHub-issued long-lived secret in `state/.credentials`.

## 6. Confirm the listener

```bash
./deploy.sh status
./deploy.sh logs --tail=200 --follow
```

`deploy.sh status` reports the runner image digest, the host
architecture and the platform it maps to, the listener container
and process state, the Docker socket reachability, the state
volume contents, the bridge network mode, the `shm_size`, the
`host.docker.internal` alias resolution, and the absence of both
`RUNNER_TOKEN` and `TITAN_RUNNER_TOKEN` from the listener
environment.

In the GitHub UI the runner appears under **Settings &rarr;
Actions &rarr; Runners** with the configured name, the
`titan-ci` custom label, and the auto-attached `self-hosted`,
`linux`, and `X64` / `ARM64` labels.

## 7. Blank the token and re-run Compose

After successful registration the token is still visible in the
runner container's environment metadata because Compose interpolates
it from `.env`. Blank the `TITAN_RUNNER_TOKEN=` line and recreate the
runner with `./deploy.sh up` so the metadata no longer carries
the token. The in-place edit MUST leave no token-bearing backup on
disk:

```bash
sed -i '/^TITAN_RUNNER_TOKEN=/d' .env
./deploy.sh up
```

Registration runs again, sees an empty `RUNNER_TOKEN` and a matching
persisted identity, exits successfully without contacting GitHub,
and the listener keeps authenticating with the long-lived secret.
The single runner container is recreated without the token.

> **Note:** the old offline GitHub runner entry may need manual
> removal from **Settings &rarr; Actions &rarr; Runners** if the
> runner was previously registered through an older
> one-shot registration workflow or with a
> different label list. The new flow registers the same runner
> identity in place, but a prior offline entry with the same
> name is not removed automatically.

## 8. Tear down (VM rotation)

When a VM is rotated, preserve the three named volumes
`titan-runner-state`, `titan-runner-work`, and `titan-runner-browser`.
The runtime tree and running container are disposable. The previous
host workspace directory is left untouched; do not copy it into the
new work volume.

```bash
# Old VM: stop the container. Named volumes remain on disk.
docker compose down

# New VM: transfer the three named volumes with a Docker volume
# backup/restore tool, then start without re-registering.
docker compose pull
docker compose up -d
```

A token is only necessary when the `state` volume is empty or
the persisted identity has drifted. The VM-level network
isolation acceptance checks in [vm-deployment.md](vm-deployment.md)
MUST also pass on the new VM before the listener starts
processing jobs.
