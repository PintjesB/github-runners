# Dedicated runner evidence storage implementation plan

> **Status:** Plan only; the client and storage service are not implemented or deployed by this PR.
> **Consumer:** [Titan Stocks PR #213](https://github.com/PintjesB/titan-stocks/pull/213), application revision `03b033608d1ee8c1974b6679137ea32d5281d121`.

**Goal:** Adapt the dedicated custom runner containers so Titan CI can persist and retrieve mandatory evidence without GitHub artifact storage.

**Architecture:** A runner-installed client writes immutable bundles to a separately
managed durable evidence service. The service owns retention, atomic publication,
and authenticated downloads. Run the evidence service on a separate storage host, outside the Docker daemon
accessible through the CI runner socket. Runner containers have no mount of the
archive store and cannot delete older evidence through the client API.

**Tech stack:** Existing native Linux amd64/arm64 Docker/Compose runner, Python 3.12 client and a maintained JWT validation library in the service, HTTPS service backed by a durable filesystem volume, authenticated
HTTPS reverse proxy for browser retrieval. GitHub OIDC machine identity and human
login are separate. Reuse the operator's existing TLS/identity proxy when available.

**Spec:** [Version 1 evidence contract](evidence-storage-contract.md), copied from
the application revision above. Keep both repositories aligned when the interface changes.
Runner source inspected at `PintjesB/github-runners` commit
`539bd8048db7188b8176f4b8883744f2dd721b47` (branch `main`). Refresh and review differences
before execution; this documentation PR changes no runner code or deployment.

## Global constraints

- Preserve both native linux/amd64 and linux/arm64 runner images and probes.
  Titan Stocks currently selects `self-hosted, linux, ARM64, titan-ci`; do not retarget
  its jobs or remove the runner repository's supported amd64 platform.
- Preserve bridge networking, host-gateway access, registration-token hygiene,
  runner-state/work/browser/Codex volumes, and scoped post-job cleanup.
- Client location `/usr/local/bin/titan-evidence`; interface version 1 as in the spec.
- API/store authorization restricted to `PintjesB/titan-stocks`; never trust a
  caller-supplied repository name as authentication. No delete/list-all API for jobs; machine GET only within the verified run/attempt.
- Retention: required-ci 14 days; full-certification/live-provider 30; runtime-sbom 90.
- No public evidence, credentials in images/environment metadata/logs/URLs, GitHub
  upload fallback, production deployment, or existing-volume deletion.
- Operator must select the actual persistent host/NAS path, TLS hostname, identity
  provider and backup destination before deployment. Never substitute runner work
  or temp directories or silently create storage on a missing NAS mount.

## Review focus

- Concurrent writes/retries: publish complete immutable objects, never partial files.
- Malicious keys/paths/symlinks: reject traversal and cross-repository access.
- Disk-full/disconnected storage: fail closed, no successful local-cache fallback.
- Container replacement/cleanup: evidence and retention metadata survive.
- Credential rotation and restore: old evidence remains retrievable and verifiable.

### Task 1: Durable evidence service and authorization boundary

**Create:** `runners/titan/evidence/server.py`, `runners/titan/evidence/Dockerfile`,
`runners/titan/evidence/compose.yml` (separate storage host),
`runners/titan/tests/test_evidence_service.py`.

**Interface:** `PUT /v1/objects/KEY`, streaming gzip bytes with a short-lived GitHub OIDC token
and retention-days header; `GET /v1/objects/KEY` for run-scoped OIDC machine readers;
authenticated human `GET /downloads/KEY`. Return the spec's JSON receipt on PUT.
Health endpoint exposes readiness only, never inventory, credentials or paths.

- [ ] Add failing tests for malformed keys, wrong repository, unauthenticated requests,
  symlinks, disallowed profiles/retention, conflicting bytes at an existing key,
  concurrent same-key writes, interrupted uploads, disk-full and missing mount.
- [ ] Validate JWTs against GitHub's issuer/JWKS with a maintained library, pinned
  algorithms, service-specific audience, exp/nbf/iat and immutable repository ID.
  Bind repository/SHA/run/attempt to verified claims. Resolve allowed job/profile
  from verified check-run/workflow metadata (not caller arguments); fail closed if
  metadata is unavailable. Enforce trusted runner admission before dispatch and
  reject fork/untrusted runs. Add tests for expired/forged/wrong-audience tokens,
  another run's key, another attempt's reads, wrong workflow/profile, replayed
  cross-run writes and forged environment variables. Dependabot identity behavior
  must be tested explicitly; do not globally accept its `dynamic` event claim.
- [ ] Implement bounded streaming and SHA-256 calculation, atomic no-clobber publication,
  file and directory fsync, and durable retention metadata. Place staging files on
  the same filesystem. Set a 512 MiB upload limit and a 180-second request deadline;
  reject oversized requests before exhausting storage. Do not expose arbitrary paths.
- [ ] Require a pre-provisioned storage marker and mount check at startup; refuse a
  missing backend rather than writing to the container layer. Serve downloads as
  attachment/octet-stream with nosniff, never execute/render uploaded HTML reports.
- [ ] Verify equal retries preserve or extend retention, conflicting retries return
  failure, partial uploads are invisible, and interrupted metadata publication can
  recover without accepting an inconsistent object.
- [ ] Run `python -m pytest runners/titan/tests/test_evidence_service.py` and commit.

### Task 2: Runner client and container capability

**Create:** `runners/titan/scripts/titan-evidence`,
`runners/titan/tests/test_evidence_client.py`.
**Modify:** `runners/titan/Dockerfile`, `runners/titan/scripts/pre-job.sh`,
`runners/titan/scripts/probe.sh`, `runners/titan/tests/test_runner_contract.py`.

**Consumes:** Task 1 API; GitHub job OIDC request URL/token supplied by Actions; no
long-lived storage credential in the runner. **Produces:** exact put/get CLI and JSON contract in the application spec.

- [ ] Add failing tests for both CLI operations, receipt types, invalid TLS,
  redirects to another host, network timeout, HTTP failures, missing OIDC capability,
  interrupted download, refused output overwrite and secret-safe errors.
- [ ] Implement HTTPS-only authenticated streaming client with certificate validation,
  no redirects, 180-second total deadline, and no shell interpolation. Request a fresh audience-bound token for each operation; never persist it or echo its request credentials. `get` must read
  from the service, not an upload cache. Create output with exclusive regular-file
  semantics; delete only its own partial download on failure.
- [ ] Install as root-owned mode 0755; keep endpoint/audience configuration outside the
  image. Preserve the pre-job hook's no-network promise: check executable/non-secret config
  availability there; exercise actual storage in the explicit capability probe.
- [ ] Run client tests and existing runner contracts. Prove the client works as the
  unprivileged runner UID with no token appearing in Docker Config.Env, then commit.

### Task 3: Compose, authenticated retrieval, retention and recovery

**Modify:** `runners/titan/docker-compose.yml`, `runners/titan/.env.example`,
`runners/titan/docs/operations.md`, `runners/titan/docs/security.md`,
`runners/titan/docs/upgrade-and-rollback.md`, `runners/titan/README.md`,
`runners/titan/tests/check_compose_contract.py`.
**Create:** `runners/titan/evidence/retention.py`,
`runners/titan/tests/test_evidence_retention.py`.

- [ ] Add Compose contract tests requiring the service storage volume in its separate
  storage-host Compose file, no archive mount or long-lived storage secrets in the
  runner Compose file, and no public unauthenticated port. Do not deploy the service
  on the Docker daemon accessible from the CI runner socket: jobs could bypass the
  API through Docker. Persist the store outside every container lifecycle.
- [ ] Wire service TLS and human authentication through the operator-selected proxy.
  Separate OIDC machine authorization from human login. Validate the full run scope
  server-side; job tokens must not grant historical reads, deletion or admin.
- [ ] Add retention tests: server-time deadlines, all four profile durations,
  legal holds, minimum retention, in-flight readers/writers, and unrelated files.
  Implement a service-side narrowly scoped dry-run-first expiry job; never run it
  from PR code or use general Docker prune. Require operator approval before enabling
  expiry against existing evidence. Emit sanitized capacity/failure metrics.
- [ ] Document backup scheduling, backup encryption/access, restore procedure and
  capacity alerts. Restore a sample to a separate disposable location, download it
  through authentication, and compare SHA-256. Prove container restart/replacement
  and runner post-job cleanup leave stored evidence available.
- [ ] Run the complete runner test suite, review final diff independently and commit.

### Task 4: Coordinated rollout and application validation

**Modify:** `runners/titan/VERSION` and deployment documentation once accepted.

- [ ] Build/test the new custom runner image on both supported native architectures without deploying it. Record its digest
  and the evidence-service image digest. Present actual host paths, OIDC trust
  provisioning, TLS/authentication configuration, backups and rollback to the operator.
- [ ] After deployment approval, drain the dedicated runner, provision the backend,
  deploy by immutable digests, and configure `TITAN_EVIDENCE_BASE_URL` in the application
  repository. Verify application validation jobs have the approved `id-token: write`
  permission and enforce the server trust policy before accepting tokens. Rotate service backend credentials using overlap/test/revoke,
  never by printing values; there must be no shared runner storage credential.
- [ ] Run explicit put/get from the runner; verify human authenticated retrieval and
  denied anonymous/cross-repository access. Test interrupted write, service outage,
  checksum mismatch and lost mount on disposable fixtures, not production storage.
- [ ] Run the Titan application evidence branch's Required CI. Verify the job summary
  URL and archive manifest independently. Run weekly certification and the SBOM
  packaging path in an evidence-only fixture; do not publish/deploy runtime images
  merely to test storage. Live-provider tests require their existing opt-in approval.
- [ ] Keep old snapshots/stashes/branches and #192 untouched. Propagate the accepted
  application workflow commit into retained PRs and rerun required CI before merging.
  If storage integration fails, keep merges/publication blocked; never waive uploads.
- [ ] Roll back runner digest and workflow together only if the GitHub artifact quota
  is resolved. Preserve new evidence storage/backups for the full retention windows.

## Acceptance and handoff

Application unit tests against a fake client are contract tests, not proof of a
working storage deployment. Completion requires real authenticated put/get,
independent checksum verification, restart persistence, restore evidence and green
application CI. This plan deliberately leaves deployment to the dedicated runner
owner; its physical storage/authentication choices are prerequisites, not defaults.

## Scope of this documentation PR

Only the Titan profile's implementation plan, shared contract and documentation index
change here. The Oportunist profile, image versions, workflows, registration and
Compose deployments are unchanged. Implement this plan in a separate reviewed PR;
merging these documents alone does not unblock Titan Stocks CI. The consumer PR
remains a draft pending real integration and final independent review.
