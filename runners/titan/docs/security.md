# Security

The runner is a *non-ephemeral* container with full access to
the host Docker daemon. The **VM acts as the security boundary**:
the runner container has unrestricted access to the VM's
Docker daemon, while the VM is isolated from production systems
and the rest of the private network. Treat the VM as a developer
workstation: no production credentials, no application volumes,
no production data.

The image is published as a multi-platform manifest with both
native `linux/amd64` and `linux/arm64` entries; the VM is
single-architecture. Emulation, mismatched daemons, and any
host architecture other than `x86_64` or `aarch64` are rejected
by `deploy.sh` and by the capability probe / pre-job hook so a
degraded host cannot reach the network.

## VM boundary

The VM is the dedicated, disposable security boundary. It is
**not** a development host, a bastion, or a shared resource.
Network isolation is enforced *outside* Compose by the VM
platform and host firewall. The Compose contract, the runner
container, and the host Docker daemon are the *inner* half of
the boundary; the VM platform is the *outer* half.

Five rules apply:

* **One repository trust boundary per VM.** Keep one heavy listener.
  The opt-in light listener may run concurrently only for the same trusted repository,
  with separate state/work/cache/browser/Codex volumes and light-only labels.
  Its cleanup hooks are disabled to protect the active heavy job. A listener
  for another repository requires a separate VM. Separate volumes protect
  normal operation; direct Docker socket access does not isolate a compromised
  listener from its sibling or the VM.
* **No production data on the VM.** The VM MUST NOT store
  production databases, application secrets, deployment
  credentials, customer data, or any workload that is unrelated
  to CI. The `titan-runner-state` volume is the GitHub-issued
  runner identity and contains no application data.
* **Deny inbound by default.** The VM firewall MUST deny every
  inbound TCP/UDP port except the explicitly approved management
  path (typically SSH from a bastion or operator IP). Ephemeral
  Docker-published ports are bound to the loopback or the
  bridge network and are not reachable from outside the VM.
* **Allow only the documented outbound destinations.** The VM
  firewall MUST allow outbound traffic to GitHub Actions
  (`*.actions.githubusercontent.com`, `*.github.com`),
  the GitHub Container Registry (`ghcr.io`), package
  registries required by the image build (deb.nodesource.com,
  cli.github.com, the Ubuntu archive), DNS, and NTP. All other
  outbound traffic is denied.
* **Block private ranges, link-local services, and cloud
  metadata.** The VM firewall MUST drop traffic to RFC 1918
  ranges (10/8, 172.16/12, 192.168/16), link-local
  (169.254/16, including `169.254.169.254` for cloud metadata
  services), `fe80::/10`, `fc00::/7`, and the VM's
  host-management / control-plane networks. The Docker
  `DOCKER-USER` chain MUST apply the same egress restrictions to
  container-forwarded traffic so a workflow service container
  cannot reach the host-management network or the cloud
  metadata service. The chain restrictions are scoped to traffic
  leaving the VM's physical interface so Docker bridges and
  `host.docker.internal` continue to work inside the listener.

Rebuild the VM if runner integrity is in doubt. The container
boundary is *not* the recovery boundary. After a suspected
compromise, revoke both listeners and discard the VM, credentials, and
all runner-owned volumes; never copy them into the replacement.
The new VM registers normally with fresh state and a fresh
token.

The Docker access path inside the VM is intentionally direct:
the runner container bind-mounts the VM's `/var/run/docker.sock`
read/write and reads the daemon through the socket. A
socket-proxy, a Docker-in-Docker (DinD) daemon, a Docker API
TCP port, and TLS certificate infrastructure are explicitly
forbidden because they would re-introduce the DinD plan that
the VM-as-security-boundary model explicitly replaces. The VM
is the security boundary; the container reaches the daemon
through the VM socket directly.

## Container boundary

The Compose contract enforces five separate guarantees inside
the VM:

* `no-new-privileges:true` blocks setuid escalation inside the
  container. The runner never needs `privileged: true`;
  privileged mode, DinD daemons, socket proxies, and the
  Docker API TCP port are explicitly forbidden. The runner
  reaches the daemon through the direct read/write bind mount
  of `/var/run/docker.sock`.
* The host socket's group ID is granted to the runner user
  inside the container as a **supplemental** group (the
  user-mod mapping uses `usermod -a -G`); the runner user's
  *primary* group remains `runner`. This keeps Docker access a
  separate capability from every other operation the runner
  performs.
* The listener runs on an ordinary Compose bridge network
  (`titan-runner-net`). It does not share the host network
  namespace; reachability to host services goes through the
  documented `host.docker.internal` alias. The listener does
  not share the host IPC namespace either.
* An isolated `shm_size: 2gb` allocation gives the Playwright
  Chromium binary enough shared memory without sharing the
  host IPC namespace.
* `restart: unless-stopped` keeps the listener online through
  host reboots; nothing else automatically restarts the
  container.

The Docker daemon's reported architecture MUST match the
native runner architecture (`amd64` or `arm64`). The capability
probe (run by `deploy.sh probe` and by the publish workflow on
each native architecture) and the pre-job hook (run on every
workflow job through `ACTIONS_RUNNER_HOOK_JOB_STARTED`) refuse
an emulated or mismatched daemon before the rest of the contract
runs. The pre-job hook reads the documented `RUNNER_ARCH` env
var that GitHub supplies (`X64` on x86_64 listeners, `ARM64`
on aarch64 listeners) and verifies that the Docker daemon
matches.

The Docker socket grant is effectively root access inside the
VM. Operators must:

* Keep the VM OS and the Docker daemon patched.
* Restrict SSH to the VM to operators only.
* Never reuse the VM for staging or production workloads.
* Block inbound access to ephemeral Docker-published ports at
  the VM firewall; bridge networking does not replace VM
  firewalling.
* Scope the `DOCKER-USER` chain restrictions to traffic
  leaving the VM's physical interface so Docker bridges and
  `host.docker.internal` continue to work.
* Enforce the documented egress restrictions on the VM and on
  the Docker `DOCKER-USER` chain.
* Rebuild or replace the VM if runner integrity is in doubt;
  never treat a single container as the recovery boundary.

## Registration token handling

The token is the only credential the operator must protect at
runtime. Six rules apply:

* The token lives in a gitignored, mode-`0600` `.env` file
  consumed by the documented `deploy.sh` allowlist. The
  allowlist parser refuses shell-unsafe values.
* `./deploy.sh up` (which invokes Compose) interpolates the token into the single
  runner container as `RUNNER_TOKEN` for its startup registration
  phase. The literal
  token value never appears as a `docker run --env` argument
  or in a log line.
* `register.sh` unsets `RUNNER_TOKEN` immediately after
  `config.sh` returns and traps the unset on every exit path.
* The startup shell unsets `RUNNER_TOKEN` before it execs the
  long-running listener. The listener process therefore never
  receives the token and authenticates with the GitHub-issued
  long-lived secret persisted in `state/.credentials`.
* Registration is idempotent: a matching persisted identity
  exits successfully without contacting GitHub and without
  requiring a fresh token, so steady-state deployments never
  have to keep the token in `.env`. Identity drift
  (different repository URL, runner name, or label list) is
  automatically detected and a fresh token triggers a
  transactional local re-registration.
* After successful registration, blank the
  `TITAN_RUNNER_TOKEN=` line in `.env` and recreate the runner
  with `./deploy.sh up` so its metadata no longer carries
  the token. The blank-and-rerun flow MUST use an in-place edit
  that leaves no token-bearing backup file on disk. The next
  startup registration sees an empty `RUNNER_TOKEN` and matching
  persisted identity, exits successfully without contacting GitHub,
  and then launches the listener.

The persistent state volume is mounted only on the runner container.
The internal registration phase writes `.credentials*` into that
volume, and the subsequent listener authenticates through the
registered GitHub identity.

## Registration label contract

The custom-label list shrinks to `TITAN_RUNNER_LABELS=titan-ci`.
GitHub automatically attaches the `self-hosted`, `linux`, and
`X64` / `ARM64` labels based on the listener's actual
platform; the custom-label list intentionally omits them so a
future architecture migration is a GitHub-side change rather
than a `TITAN_RUNNER_LABELS` rotation. Application workflows
target `[self-hosted, linux, titan-ci]` so the same selector
matches every compatible `titan-ci` listener regardless of
architecture.

Existing ARM64 runners must re-register once because changing
`TITAN_RUNNER_LABELS` from the old architecture-specific list
(`self-hosted,linux,ARM64,titan-ci`) to `titan-ci` is
intentional identity drift: the old custom-label list is being
removed rather than augmented. Set a fresh `TITAN_RUNNER_TOKEN`
and run `./deploy.sh up` so the startup registration phase
re-registers against GitHub with the new label list. The new
AMD64 VM registers normally with fresh state and a fresh
token.

## Registration serialisation

Concurrent registration attempts are serialised through an
exclusive flock on `state/.lock/register.lock` so two
`./deploy.sh up` invocations cannot race a credential
replacement. Registration runs in the same startup process as the
listener, so a failed registration exits the single container before
the listener is launched.

A re-registration takes a transactional local backup: the
existing credentials are copied into `state/.backup-<epoch>`
before the new registration runs. An ordinary `config.sh
--replace` commit error restores the previous local
credentials so a broken registration cannot overwrite working
persisted state. The local rollback is best-effort; the
GitHub-side runner record is intentionally NOT
transactionally restored after `config.sh --replace`.
Operators remove any stale runner record from the GitHub UI
manually if a partial commit leaves a remote entry behind.

## Pre-job hook

`PreJob.sh` is wired through the documented
`ACTIONS_RUNNER_HOOK_JOB_STARTED` environment variable so
GitHub Actions actually invokes it on every job. The script
reads the `RUNNER_ARCH` env var that GitHub supplies on every
job (`X64` on x86_64 listeners, `ARM64` on aarch64 listeners),
maps it to the `amd64` / `arm64` aliases Docker reports, and
validates that required host capabilities and the
`host.docker.internal` alias are available before the job
starts. The Docker daemon's reported architecture MUST match
the native runner architecture; an emulated or mismatched
daemon aborts the hook before the rest of the contract runs.
A failed check exits non-zero so the runner reports the job
as failed rather than letting it run on a degraded VM. The
hook never modifies VM state and never reaches out to the
network.

## Post-job hook

`PostJob.sh` is wired through the documented
`ACTIONS_RUNNER_HOOK_JOB_COMPLETED` environment variable. It
performs a bounded cleanup after every workflow job:

* It tears down Compose projects whose name matches the
  documented Titan CI prefix `titan-stocks-playwright-` only,
  identified through the `com.docker.compose.project` label.
  `docker compose down -v` removes each project's containers,
  its bridge network, and its project-owned volumes; external
  runner volumes are never removed.
* It removes anonymous Docker volumes whose
  `com.docker.compose.project` label matches the same prefix
  (defensive catch-all for volumes whose owning container was
  force-removed without a matching `docker compose down`).
* It does NOT recurse into the runner's `_work` directory.
  The bounded cleanup only targets the matching Compose
  projects (anchored on the documented
  `titan-stocks-playwright-` prefix); any non-Titan checkout,
  any application development volume, and any unrelated
  workspace is left alone.
* The runner container itself (`titan-runner`), the
  persistent `titan-runner-state`, `titan-runner-work`, and
  `titan-runner-browser` named volumes, the application
  `titan_postgres` and `titan_data` named volumes, and any
  non-CI Titan workload are untouched.
* It never invokes `docker system prune`, `docker volume
  prune`, `docker image prune`, `docker builder prune`, or
  `docker network prune`.
* A cleanup failure is logged but never fails the workflow
  job; the hook exits zero unless something fundamentally
  unsafe happens.

## Lifecycle lock

`deploy.sh up` and `deploy.sh down`
take an exclusive `flock` on `/var/lock/titan-runner.lock`.
Two operators or automations cannot execute these commands
simultaneously, so startup registration cannot race with an active
`up`/`down` sequence.

## State directory hardening

The persistent `state` directory is owned by `runner:runner`
with mode `0750`. `.runner` is `0640`; `.credentials` and
`.credentials_rsaparams` are `0600`. The disposable runtime
tree is rebuilt on every start with the same ownership
pattern.

The `diagnostics.txt` summary that lives alongside the
credentials contains only public metadata (repository URL,
labels, paths, runner version). It is also the source of truth
for the registration idempotency check. It never contains the
credentials, the registration token, or any application data.

## Image provenance

The `publish` workflow runs on GitHub-hosted `ubuntu-24.04`
and `ubuntu-24.04-arm` native runners, builds the real
`linux/amd64` and `linux/arm64` images on their native
architectures, gates each candidate on its native capability
probe, merges the two exact digests into a commit-specific
multi-platform manifest, probes the merged manifest on both
native architectures, then promotes the verified digest to
`ghcr.io/pintjesb/titan-stocks-runner:latest`. The immutable
multi-platform manifest digest surfaces in the workflow
output so the host can pin
`ghcr.io/pintjesb/titan-stocks-runner@sha256:<digest>` and
ignore the mutable `:latest` tag.

The runner image is intentionally public. Anonymous pulls from
ghcr.io are the only image-verification path; the workflow
does not attach keyless cosign signatures, SBOM attestations,
or any other provenance artifact.

## Source-code reuse

The project does not ship a `LICENSE` file. Pulling the image
does not grant any reproduction or reuse rights; the source
code, documentation, and configuration in this repository are
the only authoritative copies and they do not carry an
open-source license.
