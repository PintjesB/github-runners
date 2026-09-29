# Self-hosted CI evidence contract (version 1)

> Proposed contract for [Titan Stocks PR #213](https://github.com/PintjesB/titan-stocks/pull/213) at `03b033608d1ee8c1974b6679137ea32d5281d121`. This documentation PR does not install or deploy storage.

Titan Stocks PR #213 proposes running CI on its dedicated self-hosted runner and storing evidence outside
GitHub Actions artifact storage. This is an application/runner interface change:
the workflows require the runner implementation before they can pass. Installing
the application PR alone does not resolve the storage incident.

## Ownership and interface

The application owns `scripts/ci_evidence.py`, report selection, bundling,
checksums, read-back verification, and GitHub job-summary links. The runner
repository, [PintjesB/github-runners](https://github.com/PintjesB/github-runners),
owns the storage client, credentials, durable backend, retrieval service,
retention enforcement, and backups. The older `titan-stocks-runner` URL redirects
there. The adaptation plan is
[evidence-storage-plan.md](evidence-storage-plan.md).

The runner installs a root-owned executable `/usr/local/bin/titan-evidence`,
not a binary taken from the checked-out application or its PATH. Its version 1
interface is:

```
titan-evidence put --file ABSOLUTE_FILE --key KEY --retention-days DAYS
titan-evidence get --key KEY --output ABSOLUTE_FILE
```

`put` exits zero only after the immutable object and its retention metadata are
durable. It emits exactly one JSON object on stdout:

```json
{"version":1,"key":"KEY","sha256":"64 lowercase hex characters","size":123,"url":"https://evidence.example.invalid/downloads/KEY"}
```

`get` downloads the stored object, not the original input or a local upload cache,
to a newly created regular file and returns zero only when complete. Other
outcomes are nonzero. Both commands must finish within 180 seconds, must not
print secrets, and must reject overwrite attempts with different bytes.
An identical retry may succeed without reducing the original retention deadline.

KEY is `OWNER/REPO/SHA/RUN_ID/RUN_ATTEMPT/JOB/PROFILE.tar.gz`. SHA is the 40-character
GitHub checkout revision (a synthetic merge commit on PR runs); attempts are
separate namespaces. Allowed profiles and retention are `required-ci` (14 days),
`full-certification` (30), `live-provider` (30), and `runtime-sbom` (90).

Set the non-secret repository Actions variable `TITAN_EVIDENCE_BASE_URL` to the
HTTPS authenticated download prefix (no trailing query, fragment, or credentials).
The receipt URL must equal that prefix followed by `/KEY`. Authentication happens
through the retrieval service's login/session, never tokens in a job-summary URL.
The public example domain above is documentation only, not a configured backend.

The planned client authenticates with short-lived GitHub OIDC tokens; there is no
shared runner-readable storage password. The service must validate the issuer,
audience, signature, expiry, immutable repository ID, workflow, run/attempt and
checkout SHA. It must derive or authorize the key from those verified claims and
allow machine read-back only for that run, never arbitrary historical evidence.
The server must resolve/authorize the job/profile using verified check-run/workflow
metadata rather than trusting command arguments. Human access uses separate login.
See [GitHub's OIDC reference](https://docs.github.com/en/actions/reference/security/oidc).

Required CI, certification and live-provider jobs have `id-token: write` so the
client can request a short-lived identity token. This does not grant repository
write access. The publisher already has the permission for signing. The service
must enforce the claim restrictions above; token availability is not authorization
to read other runs or write arbitrary object keys.

Run the storage service outside the Docker daemon accessible from the runner;
otherwise job code could bypass the storage API through the Docker socket.

Fork/untrusted PRs must not execute on the privileged persistent runner. The runner
owner must enforce admission before dispatch and test rejection; GitHub OIDC alone
does not sandbox PR code or make a Docker-socket-enabled runner safe for it.

## Evidence and failure behavior

Bundles contain only the existing report allowlists and `manifest.json` (identity,
job status, file sizes and SHA-256 hashes). Symlinks and non-regular files are
rejected using no-follow directory/file descriptors. Files are snapshotted once and
the exact archived bytes are hashed. Limits are 10,000 entries, 128 MiB per file,
256 MiB total input, and 512 MiB compressed archive. Checkout's default clean behavior must remain enabled so a persistent
runner does not supply stale generated reports. No `.env`, dependency directories,
runtime data or unrestricted workspace archives are selected.

A documentation-only run may legitimately contain just the manifest. Successful
selected Python, frontend and browser suites and live-provider runs require their
report files; SBOM
publication requires both the SPDX file and checksum. Failed validation still
attempts to save partial evidence and its failure status. Storing evidence does
not turn a failing validation step green.

After `put`, the application validates the receipt, executes `get`, and compares
the downloaded archive's size and SHA-256. Only then does it append the evidence
link to the GitHub job summary. Missing client/configuration, upload failure,
invalid receipt, corrupt/missing read-back, and summary-write failures are fatal.
There is no `continue-on-error`, local-only success fallback, or GitHub upload
fallback. Docker Buildx's automatic build-record upload is disabled as well;
SBOM generation, signing, attestation, and promotion gates remain intact.
GitHub caches and GHCR images are separate from this evidence change.

## Rollout, rotation and rollback

1. Implement and test the runner plan first. Provision durable storage, authenticated
   retrieval, quota monitoring, and a restore-tested backup. Do not deploy from this PR.
2. Install the versioned client into a digest-pinned runner image; configure its
   OIDC audience and service trust policy and set the download-prefix variable. Test a real
   put/get from the runner UID and a browser download from an authorized account.
3. Run the application branch through Required CI. Preserve failed-run evidence
   too; independently download a bundle and verify its checksum/manifest. Check
   restart persistence and credential-denied/unavailable-storage failures.
4. Only merge after all required validation and the cross-repository integration
   tests pass. Update remaining retained PRs with this reviewed change and rerun
   them; existing failed runs do not become green automatically.

The client obtains a fresh OIDC token per operation and never persists tokens.
Rotate the service's own backend credentials with overlap/test/revoke outside the
runner; GitHub signing-key rotation uses validated issuer JWKS with bounded caching.
Keep human retrieval sessions and backup credentials separate. Verify old objects
remain readable after rotation; never place credentials in Docker environment metadata.

Rollback workflow changes together with the runner capability if necessary.
GitHub artifact uploads can only be restored once its quota/billing blocker is
resolved; otherwise pause merging/publication. Keep the self-hosted evidence and
its backups readable for their full retention windows. No volume deletion,
artifact purge, branch deletion, migration, or production deployment is part of
this change. Existing recovery snapshots and excluded PR #192 remain untouched.
