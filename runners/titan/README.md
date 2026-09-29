# titan-stocks-runner

Public repository that owns the persistent multi-platform GitHub
Actions runner for
[PintjesB/titan-stocks](https://github.com/PintjesB/titan-stocks).

* **Image** &mdash; an Ubuntu 24.04 multi-platform image (native
  `linux/amd64` and `linux/arm64`) that carries the Docker CLI,
  Compose v2 plugin, Buildx, the GitHub CLI, ShellCheck, PostgreSQL
  client, Node 24, Python 3.12, the Playwright Chromium system
  dependencies (`libcairo2` and `libpangocairo` included), `gosu`,
  and the GitHub Actions Runner binary pinned by architecture-
  specific SHA-256 digests.
* **Lifecycle** &mdash; a small set of shell scripts that operators
  invoke on the dedicated CI VM through `deploy.sh` (`build`,
  `probe`, `up`, `down`, `status`, `logs`).
* **Contract tests** &mdash; focused Python and shell tests that pin
  the documented operator contract.
* **Publishing** &mdash; a `publish` workflow that builds the real
  `linux/amd64` and `linux/arm64` images on native hosted runners,
  gates each candidate on its native capability probe, merges the
  two exact digests into a commit-specific multi-platform manifest,
  probes the merged manifest on both native architectures, then
  promotes the verified digest to `latest` and reports its
  immutable manifest digest. The CI surface is a hosted
  `ubuntu-24.04` / `ubuntu-24.04-arm` build matrix; the published
  digest surfaces in the workflow output so deployments pin
  `latest@sha256:<digest>`.

The image is publicly pullable. Running the container still requires
the operator to supply a GitHub Actions registration token *once*;
the project does not include any reusable source code, so it ships
without a `LICENSE` file and without an OCI license label. **No
reproduction or reuse rights are granted by publication.**

## Architecture at a glance

The image is published as a single multi-platform manifest that
contains a native `linux/amd64` and a native `linux/arm64` build.
`deploy.sh` maps the host's `uname -m` output to the matching
`linux/amd64` or `linux/arm64` platform so a build, a probe, and a
`docker run --platform` invocation always resolve to a native
architecture. Emulation, mismatched daemons, and any host
architecture other than `x86_64` or `aarch64` are rejected up front
so a degraded deployment cannot reach the network.

An application job runs once on whichever compatible `titan-ci`
runner is available. The dedicated VM remains the security boundary:
the runner mounts the VM's Docker socket directly and reaches the
daemon as a supplemental group on the runner user.

The runner separates immutable image contents, persistent runner
identity, and disposable materialised runtime so a container can be
recreated without touching GitHub:

```
GHCR image                        /opt/actions-runner
  immutable binaries and tools    (read-only image layer)
        |
        v
runner container
  disposable materialised tree    /var/lib/titan-runner/runtime/
        |  (cp -a $RUNNER_ROOT/. $RUNTIME_DIR at every start)
        |
        +--- titan-runner-state volume      GitHub identity
        |    /var/lib/titan-runner/state/
        |      .runner
        |      .credentials
        |      .credentials_rsaparams
        |      .lock/register.lock          registration flock
        |      diagnostics.txt
        |
        +--- titan-runner-work volume        runner workspace
        |     /var/lib/titan-runner/work     (fixed path; starts empty
        |                                      on migration)
        |
        +--- titan-runner-browser volume    Playwright cache
        |                                    seeded from the baked
        |                                    image cache on first start
        |
        +--- /var/run/docker.sock/          host bind mount
                                             daemon access as supplemental group

  bridge network                   titan-runner-net
                                    + host.docker.internal -> host-gateway
                                    /dev/shm isolated, sized 2gb
```

The stack exposes exactly one service and one steady-state
container: `runner` (`titan-runner`). Its startup entrypoint runs the
idempotent registration phase in-process, then launches the listener.
A matching persisted identity exits successfully without contacting
GitHub; a missing or drifted identity requires a fresh token and
prevents the listener from starting until registration succeeds.

## Quick start (dedicated CI VM)

The runner is deployed on a dedicated, disposable CI VM. The
**VM is the security boundary**: the runner container has
unrestricted access to the VM's Docker daemon, while the VM is
isolated from production systems and the rest of the private
network. The deployment is reproducible from a single `.env`
file. The VM only needs Docker Engine with the Compose v2
plugin and Buildx on `PATH`; Compose owns the persistent volumes
so no manual host-directory preparation is required. The
VM-level network isolation contract is documented in
[docs/vm-deployment.md](docs/vm-deployment.md).

1. Copy the committed `.env.example` and fill in the deployment
   variables:

   ```bash
   cp .env.example .env
   chmod 0600 .env
   ```

   `.env` is gitignored. `docker compose` reads the documented
   deployment variables from it; only the entries the Compose
   contract explicitly interpolates reach the services. Arbitrary
   keys in `.env` are not propagated. The file must remain mode
   `0600` or stricter because `TITAN_RUNNER_TOKEN` lives inside it.

2. Fetch a short-lived registration token from GitHub and store it
   on the `TITAN_RUNNER_TOKEN=` line of `.env`:

   ```bash
   gh api -X POST \
     repos/PintjesB/titan-stocks/actions/runners/registration-token \
     | jq -r .token
   ```

   Paste the resulting token onto the `TITAN_RUNNER_TOKEN=` line in
   `.env`. The token is forwarded by Compose to the runner startup
   entrypoint as `RUNNER_TOKEN`; it is unset before the listener is
   launched and never reaches the listener process.

3. Start the single runner container. Registration runs internally
   before the listener starts:

   ```bash
   docker compose pull
   ./deploy.sh up
   ```

   `register.sh` is idempotent: a matching persisted identity
   exits successfully without contacting GitHub and without
   requiring a token. Identity drift (different repository URL,
   runner name, or label list) is automatically detected and a
   fresh `TITAN_RUNNER_TOKEN` triggers a transactional
   re-registration; the previous credentials are kept under
   `state/.backup-<epoch>` until the new ones are validated.

4. Confirm the listener is online and registered:

   ```bash
   ./deploy.sh status
   ./deploy.sh logs --tail=50
   ```

5. Blank the `TITAN_RUNNER_TOKEN=` line in `.env` (the documented
   blank-and-recreate flow) and re-run `./deploy.sh up` so the
   recreated runner metadata no longer carries the token:

   ```bash
   sed -i '/^TITAN_RUNNER_TOKEN=/d' .env
   ./deploy.sh up
   ```

   The in-place edit leaves no `*.bak` token-bearing backup on
   disk. `register.sh` runs again with an empty token, sees the
   matching persisted identity, exits successfully without
   contacting GitHub, and the listener keeps authenticating with
   the long-lived secret persisted in `state/.credentials`.

6. After a host reboot or a container restart the listener returns
   online without another registration token. Only an image upgrade
   or a credential rotation touches the secret store again.

## Documentation

* [docs/vm-deployment.md](docs/vm-deployment.md) &mdash; the
  dedicated CI VM contract, the VM-level network isolation
  requirements, and the acceptance checks that confirm the VM
  cannot reach protected network ranges or metadata services.
* [docs/quick-start.md](docs/quick-start.md) &mdash; the bootstrap
  recipe in narrative form.
* [docs/operations.md](docs/operations.md) &mdash; `deploy.sh`
  reference, lock file, status reporting, image upgrades, rollback.
* [docs/security.md](docs/security.md) &mdash; the VM boundary,
  the container boundary, registration-token handling, and what
  the Docker socket permissions actually mean.
* [docs/upgrade-and-rollback.md](docs/upgrade-and-rollback.md)
  &mdash; how to refresh the runner image and how to roll back to a
  previous digest.
* [docs/troubleshooting.md](docs/troubleshooting.md) &mdash; failure
  triage for the documented failure modes.

## Operator contract

| Item | Value |
| --- | --- |
| Architectures | native `linux/amd64` and `linux/arm64` only; `deploy.sh` maps the host's `uname -m` to the matching platform and aborts on any other architecture |
| Image | multi-platform manifest published as `ghcr.io/pintjesb/titan-stocks-runner:latest` with both `linux/amd64` and `linux/arm64` entries; deployments pin by digest |
| VM | dedicated, disposable CI VM; the VM is the security boundary; no production data, no application secrets, no unrelated workloads |
| VM network | deny inbound by default; allow outbound to GitHub, GHCR, package registries, DNS, and NTP; block RFC 1918, link-local, and cloud metadata; the same egress restrictions apply to the Docker `DOCKER-USER` chain |
| Container network | bridge network `titan-runner-net`; the listener does **not** share the VM network namespace |
| Host gateway | `host.docker.internal:host-gateway` wired through `extra_hosts`; workflow service containers and Compose-published HTTP services are reached through this alias |
| IPC | isolated `shm_size: 2gb`; the listener does **not** share the VM IPC namespace |
| Privileges | `no-new-privileges:true`; `privileged: false`; no DinD, no socket proxy, no Docker API TCP port |
| Docker socket | bind-mounted read/write directly from the VM; the socket's VM GID is granted as a *supplemental* group to the runner user inside the container; the daemon's reported architecture MUST match the native runner architecture (emulated/mismatched daemons are rejected by both the capability probe and the pre-job hook) |
| Registration | Internal startup phase of the single `runner` service; idempotent (a matching persisted identity exits without contacting GitHub); serialised through `state/.lock/register.lock`; identity drift automatically re-registers with a fresh token before the listener starts |
| Registration labels | `TITAN_RUNNER_LABELS=titan-ci` (custom labels only); GitHub automatically attaches `self-hosted`, `linux`, and `X64`/`ARM64` based on the listener's actual platform |
| Registration token | `TITAN_RUNNER_TOKEN` forwarded by Compose to the runner startup entrypoint as `RUNNER_TOKEN`; unset before the listener is launched and never available to the listener process or persistent state. Local rollback on ordinary `config.sh` errors is best-effort; the GitHub-side runner record is not transactionally restored after `config.sh --replace` |
| Listener env | The startup shell consumes and unsets `RUNNER_TOKEN` before execing the listener; the runner then authenticates with the GitHub-issued long-lived secret persisted in `state/.credentials` |
| State | `titan-runner-state` volume holds the Actions runner credentials |
| Runtime | disposable materialised tree at `/var/lib/titan-runner/runtime/`; rebuilt on every container start |
| Work | named volume `titan-runner-work` mounted at the fixed `/var/lib/titan-runner/work` path; it starts empty on migration and persists runner checkouts; child Compose services must mount this volume externally at the same path |
| Browser | `titan-runner-browser` volume holds the Playwright cache; seeded from the baked image cache on first start |
| Hygiene | pre-job hook validates VM capabilities and confirms the Docker daemon architecture matches the native runner architecture (`RUNNER_ARCH=X64`/`ARM64`); post-job hook tears down only `titan-stocks-playwright-` Compose projects (containers, networks, project-owned volumes); never recurses the runner's `_work` directory or removes external runner volumes; never runs a global prune; hooks activated through `ACTIONS_RUNNER_HOOK_JOB_STARTED` and `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` |
| Lifecycle lock | `flock /var/lock/titan-runner.lock` around `up` and `down`; `register.sh` additionally holds `state/.lock/register.lock` so startup registration is serialised |
| Image pin | every deployment references `ghcr.io/pintjesb/titan-stocks-runner@sha256:<digest>` |
| VM rotation | rotate the VM by preserving the `titan-runner-state`, `titan-runner-work`, and `titan-runner-browser` volumes; the old host workspace directory is left untouched and no new token is required |

### Child-job workspace contract

Workflow Compose files that need checkout files inside a child
container must use the runner's named workspace volume explicitly:

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

Reference checkout paths below `/var/lib/titan-runner/work`; host-path
bind mounts and workspace path overrides are not part of the deployment
interface.

## Repository hygiene

* No LICENSE file. The image is publicly pullable; no reuse rights
  are granted.
* No OCI license label.
* The Compose contract is generic &mdash; it accepts the consumer
  repository URL, runner name, and labels through deployment
  environment variables. The Titan Stocks repository is the default
  in the documentation examples; nothing else hardcodes that
  relationship.
* `.dockerignore` excludes everything except the scripts and
  Dockerfile so every published layer is minimal.
* The hosted `publish` workflow runs on `ubuntu-24.04` and
  `ubuntu-24.04-arm` native runners, builds and probes each
  architecture on its native runner, merges the two exact digests
  into a commit-specific multi-platform manifest, probes the merged
  manifest on both native architectures, then promotes the
  verified digest to `:latest`. CI does not depend on the runner
  image itself.

## Related repositories

* [PintjesB/titan-stocks](https://github.com/PintjesB/titan-stocks)
  &mdash; the private consumer application; every workflow targets
  the `[self-hosted, linux, titan-ci]` label group.

## Self-hosted CI evidence storage

Both runner profiles install the versioned `titan-evidence` client. See the
[service runbook](evidence/README.md) and [consumer contract](docs/evidence-storage-contract.md).
The service requires separately provisioned durable storage, HTTPS and human
authentication before [Titan Stocks PR #213](https://github.com/PintjesB/titan-stocks/pull/213)
can pass its required evidence gates. Image installation alone does not deploy storage.
