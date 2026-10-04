# Operations

This document is the operator-facing reference for `deploy.sh`,
`docker compose`, and the persistent runner. The runner is
deployed on a dedicated CI VM that itself is the security
boundary; the VM network isolation contract is documented in
[vm-deployment.md](vm-deployment.md) and pinned at the top of
[security.md](security.md).

## Multi-platform image

The published image is a single multi-platform manifest that
contains a native `linux/amd64` and a native `linux/arm64`
build. `deploy.sh` maps the host's `uname -m` output to the
matching `linux/amd64` or `linux/arm64` platform so a build, a
probe, and a `docker run --platform` invocation always resolve
to a native architecture. The Docker daemon's reported
architecture MUST match the native runner architecture; an
emulated or mismatched daemon is rejected by both the
capability probe and the pre-job hook so a degraded host cannot
pass the host-boundary contract.

The two architectures are built and probed on *native* GitHub-
hosted runners (`ubuntu-24.04` for AMD64, `ubuntu-24.04-arm`
for ARM64). The two exact candidate digests are merged into a
commit-specific multi-platform manifest; the merged manifest is
then pulled and probed on both native architectures before the
verified digest is promoted to `:latest`. Deployments pin by
digest and ignore the mutable tag.

An application job runs once on whichever compatible `titan-ci`
runner is available. The custom-label list shrinks to
`TITAN_RUNNER_LABELS=titan-ci` because GitHub automatically
attaches `self-hosted`, `linux`, and `X64` / `ARM64` based on
the listener's actual platform; the contract surfaces this as a
deliberate `TITAN_RUNNER_LABELS` rotation rather than as an
operator-configurable architecture override.

## Storage tiers

The runner separates immutable image contents, persistent runner
identity, and disposable materialised runtime. Each tier is
recreated or refreshed independently so a container restart never
has to fetch a new registration token.

| Path | Storage | Owner | Mode | Persistent? |
| --- | --- | --- | --- | --- |
| `/opt/actions-runner` | Image-owned source tree | `root` | image default | immutable |
| `/var/lib/titan-runner/state` | named volume `titan-runner-state` | `runner:runner` | `0750` | **yes** |
| `/var/lib/titan-runner/runtime` | writable dir in container | `runner:runner` | `0750` | **no** |
| `/var/lib/titan-runner/work` | named volume `titan-runner-work` | `runner:runner` | `0750` | **yes** |
| `/var/lib/titan-runner/browser` | named volume `titan-runner-browser` | `runner:runner` | `0750` | **yes** |
| `/opt/titan-probe/node_modules` | Image-owned `playwright-core` install | `root` | image default | image layer |
| `/opt/actions-runner/.hooks` | Image-owned pre/post-job hooks | `root` | `0755` | image layer |

The state directory holds `.runner`, `.credentials`,
`.credentials_rsaparams`, `.lock/register.lock` (an internal
serialisation flock), and a sanitised `diagnostics.txt`. The
runtime directory is wiped on every container recreation; the
credentials overlay it from state on the next start. The work
directory is the persistent `titan-runner-work` named volume,
mounted at the fixed `/var/lib/titan-runner/work` path. It starts
empty when migrating from the old host-directory layout; leave that
old directory untouched because GitHub recreates checkouts in the
new volume. Child-job Compose files that need checkout files must
mount this same external volume at the same fixed path:

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

## Networking

The listener runs on an ordinary Compose bridge network
(`titan-runner-net`) rather than sharing the VM's network
namespace. Service containers published by workflow jobs must
expose ports onto the Docker VM so the listener can reach them
through `host.docker.internal`. The alias maps to the VM gateway
IP through Docker's supported host-gateway mechanism
(`extra_hosts: ["host.docker.internal:host-gateway"]`).

VM firewall rules still block inbound access to ephemeral
Docker-published ports; bridge networking does not replace VM
firewalling. The `DOCKER-USER` chain restrictions apply to
traffic leaving the VM's physical interface so Docker bridges
and `host.docker.internal` continue to work inside the
listener. The VM-level network isolation contract is documented
in [vm-deployment.md](vm-deployment.md); the acceptance checks
confirm that the VM (and a disposable Docker container inside
it) cannot reach private network ranges, the host-management
network, link-local services, or the cloud metadata endpoint.

## IPC and shared memory

The listener used to share the VM IPC namespace so Playwright
could allocate the large shared-memory segments it expects.
Sharing the VM IPC namespace is replaced by an isolated
`shm_size: 2gb` allocation on the listener's own `/dev/shm`. The
Playwright Chromium binary runs entirely inside the listener's
IPC namespace.

## `deploy.sh` reference

| Subcommand | Lock | Purpose |
| --- | --- | --- |
| `build`   | none | Build the runner image for the native host architecture (`linux/amd64` or `linux/arm64`) with Buildx and load it into the local daemon. |
| `probe`   | none | Run `probe.sh` in a one-shot container with the Docker socket bind-mounted. Uses `--entrypoint /usr/local/bin/probe`. Runs on ordinary bridge networking with the native platform. Passes `EXPECTED_ARCH` so the probe can reject an emulated/mismatched Docker daemon before the rest of the contract runs. The probe sidecar never receives the registration token. |
| `up`      | exclusive | `docker compose up -d --force-recreate --remove-orphans`. The single runner container runs the idempotent registration phase first, then starts the listener. The token may be absent after the first registration because matching persisted identity skips GitHub. Identity drift automatically triggers transactional local re-registration before the listener starts. |
| `down`    | exclusive | `docker compose down --remove-orphans`. State, work, and browser volumes remain on disk. |
| `status`  | none | Report runner image digest, host architecture and platform, container state, listener process, docker socket reachability, state/work/browser volume presence, bridge network mode, `shm_size`, `host.docker.internal` alias resolution, and absence of both `RUNNER_TOKEN` and `TITAN_RUNNER_TOKEN` from the listener environment. |
| `logs`    | none | Tail the runner logs (`docker compose logs`). |

Architecture check: `deploy.sh` maps `uname -m` to `amd64` or
`arm64` and aborts immediately on any other host architecture.

Lifecycle lock: `up` and `down` take an exclusive
flock on `${TITAN_RUNNER_LOCK_FILE:-/var/lock/titan-runner.lock}`.
`status` and `logs` skip the lock so operators can inspect the
deployment while a long command runs in another shell. The
registration script itself additionally takes an exclusive flock
on `state/.lock/register.lock` so two `up` invocations cannot
race a credential replacement.

## Allowlisted env file

`deploy.sh` reads every deployment variable from the configured
`TITAN_RUNNER_ENV_FILE` (default: `.env`). The parser accepts
only documented `KEY=value` entries; arbitrary keys are silently
dropped. Values are checked against a shell-safety predicate (no
newlines, no command substitution, no quoting characters) so a
`docker run --env-file` injection is impossible. `docker compose`
itself only interpolates the entries explicitly referenced by
the `docker-compose.yml` contract under each service's
`environment:` block; the allowlist is the auditable source of
truth for which `.env` entries are deployment-relevant.

The current allowlist:

| Key | Purpose |
| --- | --- |
| `TITAN_RUNNER_IMAGE` | Pinned image digest (multi-platform manifest) |
| `TITAN_RUNNER_REPO_URL` | Consumer repository URL |
| `TITAN_RUNNER_NAME` | Display name |
| `TITAN_RUNNER_LABELS` | Custom capability label list (`titan-ci`; GitHub auto-attaches `self-hosted`, `linux`, and `X64` / `ARM64`) |
| `TITAN_RUNNER_TOKEN` | Registration token (the runner startup phase sees it; the listener process does not) |
| `TITAN_RUNNER_STATE_DIR` | Persistent state path |
| `TITAN_RUNNER_RUNTIME_DIR` | Disposable runtime path |
| `TITAN_RUNNER_BROWSER_DIR` | Playwright cache path |
| `TITAN_RUNNER_ROOT` | Image-owned runner tree path |
| `TITAN_RUNNER_STATE_VOLUME` | Named volume for state |
| `TITAN_RUNNER_LOCK_FILE` | Lifecycle lock path |

`deploy.sh` loads the allowlisted `.env` BEFORE resolving
`TITAN_RUNNER_LOCK_FILE` so the operator can override the
lock-file location through the documented allowlist.
Explicitly exported values, including deliberate empty overrides
(`export TITAN_RUNNER_TOKEN=`), always win over `.env`.

## Persistent state layout

The internal registration phase writes the following files into
`$RUNNER_STATE_DIR` (default `/var/lib/titan-runner/state`) with
strict permissions:

| File | Permission | Owner |
| --- | --- | --- |
| `.runner` | `0640` | `runner:runner` |
| `.credentials` | `0600` | `runner:runner` |
| `.credentials_rsaparams` | `0600` | `runner:runner` |
| `.runner_pkey` | `0600` | `runner:runner` (when generated) |
| `.lock/register.lock` | `0640` | `runner:runner` |
| `diagnostics.txt` | `0640` | `runner:runner` |

`diagnostics.txt` contains only the registered repository URL,
runner name, label list, paths, and the runner version. It is
also the source of truth for the registration idempotency check,
so the script can detect drift without contacting GitHub when no
token is supplied.

## Health check

The Compose contract wires a **lightweight** listener healthcheck
into the `HEALTHCHECK` directive. It verifies two observable
properties and nothing else:

* `pgrep -u runner -f 'Runner.Listener'` is non-empty &mdash; the
  listener process is alive.
* `gosu runner docker info` exits 0 &mdash; the runner user can
  reach the host Docker daemon.

The full capability probe (Compose, Buildx, Node, Python,
Playwright Chromium, ShellCheck, GitHub API,
`host.docker.internal` alias, native-architecture match)
deliberately does **not** run as the container healthcheck.
Transient dependency drift must not trigger a restart loop on a
long-lived listener; release validation is the right place for
the full probe.

## Pre-job and post-job hooks

GitHub Actions invokes `$RUNNER_ROOT/.hooks/PreJob.sh` and
`$RUNNER_ROOT/.hooks/PostJob.sh` around every job. The image
bundles these hooks in `/opt/actions-runner/.hooks/` so they
materialise into every runtime tree. The Compose contract wires
the activation mechanism &mdash; the documented
`ACTIONS_RUNNER_HOOK_JOB_STARTED` and
`ACTIONS_RUNNER_HOOK_JOB_COMPLETED` environment variables
pointing at absolute runtime paths &mdash; so the hooks actually
fire.

`PreJob.sh` resolves the native runner architecture from the
`RUNNER_ARCH` env var that GitHub supplies on every job (`X64`
on x86_64 runners, `ARM64` on aarch64 runners) and validates
that required host capabilities and the `host.docker.internal`
alias are available before the job starts. The Docker daemon's
reported architecture MUST match the native runner
architecture; an emulated or mismatched daemon aborts the hook
before the rest of the contract runs. A failure exits non-zero
so the runner reports the job as failed rather than letting it
run on a degraded VM.

`PostJob.sh` tears down only Compose projects whose name matches
the documented Titan CI prefix `titan-stocks-playwright-`
(matched through the `com.docker.compose.project` label). For
each matching project, `docker compose down -v` removes the
project's containers, its bridge network, and its project-owned
volumes. The external runner volumes (`titan-runner-state`,
`titan-runner-work`, and `titan-runner-browser`) and any application
development volumes are excluded by construction. The hook never
recurses the runner's
`_work` directory, never invokes `docker system prune`,
`docker volume prune`, `docker image prune`, `docker builder
prune`, or `docker network prune`, and never touches the runner
container (`titan-runner`) or any non-CI Titan workload.

## Image digest resolution

`deploy.sh status` resolves the registry-served manifest digest
of the configured `TITAN_RUNNER_IMAGE` by parsing the `Digest:`
line emitted by `docker buildx imagetools inspect`, and validates
that it matches `^sha256:[0-9a-f]{64}$`. The publish workflow
resolves the digest *after* promotion so the value surfaced to
operators is definitively what GHCR serves.

## Updating labels

The custom-label list is configured per host, not per image.
GitHub auto-attaches the `self-hosted`, `linux`, and `X64` /
`ARM64` labels based on the listener's actual platform; the
custom-label list shrinks to `TITAN_RUNNER_LABELS=titan-ci`
only. Adding a future architecture migration is a GitHub-side
change rather than a `TITAN_RUNNER_LABELS` rotation; existing
runners must re-register once with a fresh `TITAN_RUNNER_TOKEN`
because shrinking the custom-label list is intentional identity
drift.

When the operator wants to add additional custom labels (for
example, `titan-ci,foo` for an experiment), edit
`TITAN_RUNNER_LABELS` in `.env`, set a fresh
`TITAN_RUNNER_TOKEN`, then run `docker compose up -d`. The
internal registration phase detects the drift between the persisted and
the requested identity, takes a transactional local backup of
the existing credentials, and re-registers against GitHub. An
ordinary `config.sh --replace` commit error restores the
previously working local credentials so the listener cannot
come up with broken state. The local rollback is best-effort;
the GitHub-side runner record is not transactionally restored
after `config.sh --replace` and must be cleaned up manually if
a partial commit leaves a stale remote entry.

## Rotating the repository URL

To migrate the runner to a new repository, update
`TITAN_RUNNER_REPO_URL` in `.env`, set a fresh
`TITAN_RUNNER_TOKEN`, then run `docker compose up -d`. The
GitHub-issued secret in `.credentials` is invalidated by GitHub
whenever the registration moves between repositories, so a fresh
token is required. The startup entrypoint reloads the persisted
credentials once the re-registration completes, then launches the
listener.

> **Note:** if the previous registration is left as an offline
> entry in the GitHub UI under **Settings &rarr; Actions &rarr;
> Runners**, remove it manually. The new registration re-uses
> the same runner name and labels and cannot unregister the old
> entry through `--replace` if the two registrations have been
> tied to different accounts.

## Detecting listener health

`deploy.sh status` queries the listener container directly:

* `Runner.Listener` process running inside the container.
* Docker socket reachability: `docker info` inside the container.
* Credentials file presence inside the container's state directory.
* Bridge network mode: `.HostConfig.NetworkMode` is not `host`.
* Shared memory size: `.HostConfig.ShmSize` is the documented value.
* `host.docker.internal` alias resolution: `getent hosts`
  resolves inside the listener.
* `RUNNER_TOKEN` absence: the listener environment has zero
  occurrences of `RUNNER_TOKEN=`.
* `TITAN_RUNNER_TOKEN` absence: the listener environment has
  zero occurrences of `TITAN_RUNNER_TOKEN=`.

A red healthcheck, a missing listener process, an inaccessible
socket, or an unexpected network mode indicates the listener
cannot service jobs.

## Replacing the host

The `titan-runner-state`, `titan-runner-work`, and
`titan-runner-browser` named volumes are protected persistent
storage and must be preserved across a host rotation. The runtime
tree and running container are disposable. The previous host
workspace directory is left untouched and is not copied into the
new work volume.

```bash
# Old host: stop the stack. Named volumes remain on disk.
docker compose down

# New host: transfer the three named volumes with a Docker volume
# backup/restore tool, then bring the stack up without re-registering.
docker compose pull
docker compose up -d
```

A token is only necessary when the `state` volume is empty, the persisted
identity has drifted, or the persisted `.runner` manifest is an old
ephemeral/one-shot registration. Ephemeral registrations are deliberately
rejected even when repository URL, runner name, and labels match because GitHub
de-registers them after one job. Supply a fresh `TITAN_RUNNER_TOKEN` and run
`./deploy.sh up` once to replace that state with a persistent registration.

After a suspected compromise, revoke the runner and discard the
VM, credentials, all runner-owned volumes, and the old host
workspace directory; never copy them into the replacement. The
new VM registers normally with fresh state and a fresh token.
