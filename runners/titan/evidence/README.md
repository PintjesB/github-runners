# Durable CI evidence

Both dedicated runner images contain `/usr/local/bin/titan-evidence`. The client
uses the current job's GitHub OIDC token; runners receive no storage password and
mount no archive directory. Upload failures remain fatal to consuming workflows.

## Provisioning

Deploy this service on a separate host whose Docker daemon is inaccessible to CI
jobs. Choose an HTTPS hostname and audience before configuring workflows. Build
this directory, publish the service image to your private registry, and set
`EVIDENCE_IMAGE` to its immutable digest. The supplied Compose model only binds
127.0.0.1; an operator-managed TLS proxy must provide remote access.

Pre-provision a durable filesystem, owned by uid/gid 10001, with the marker file
`.titan-evidence-store-v1`. Bind it through `EVIDENCE_STORAGE_PATH`. The service
refuses a missing mount or marker. Copy `policy.example.json` to the path specified
by `EVIDENCE_POLICY_PATH`; check exact repository IDs, workflow paths, job names,
runner labels and retention before starting. Oportunist is deliberately denied
until its consuming workflow is configured in this allowlist.

Set `EVIDENCE_OIDC_AUDIENCE` and `EVIDENCE_DOWNLOAD_BASE_URL` (HTTPS URL ending in
`/downloads`). Supply `EVIDENCE_GITHUB_TOKEN_PATH` with a service-only GitHub token
limited to metadata and Actions read access for the allowlisted repositories.
Supply `EVIDENCE_PROXY_SECRET_PATH` with a distinct random proxy secret. Secret
files must be readable by uid 10001 and inaccessible to other users. Never place
these files or their contents in Git, runner environments, CI logs or images.

The TLS proxy must forward `/v1/objects/` with the original Authorization header.
For `/downloads/`, it must enforce the organization's human identity and access
policy, strip client-supplied `X-Forwarded-User` and `X-Evidence-Proxy-Secret`, then
inject the authenticated identity and proxy secret. The service treats this proxy
as its human authentication boundary. Never expose port 8080 directly or route
unauthenticated downloads to it. Disable request-body and Authorization logging.

Configure both runners with `TITAN_EVIDENCE_BASE_URL` and
`TITAN_EVIDENCE_AUDIENCE`; configure consuming repository Actions variables with
the same base URL. Workflows require `id-token: write` and must verify returned
receipts. Until these prerequisites are complete, required evidence gates fail
closed. This change does not deploy the service or bypass GitHub artifact gates.

## Validation and operations

Run `python -m pytest runners/titan/tests/test_evidence*.py`. The HTTPS integration
fixture exercises actual client processes, signed JWT validation, checksum
readback, service restart, backup restore and authenticated human downloads.
Native runner builds and probes execute on amd64 and arm64 in PR CI.

Uploads are capped at 512 MiB, immutable per run/job/profile key, and protected by
per-key locks. Identical retries extend retention; conflicting bytes fail. Both
machine and human downloads verify stored SHA-256 before serving content.
Retention is 14 days for required CI, 30 for certification/live-provider and 90
for SBOMs. Run `python /opt/evidence/retention.py` inside the service container for
a dry run; only the separate operator scheduler may invoke `--apply`. Legal holds
are retained. Monitor disk capacity and health; do not use CI jobs for cleanup.

Back up the entire filesystem, including metadata, with a consistent filesystem
snapshot. Restore to a separate mounted volume, recreate ownership if required,
and verify archive hashes before accepting the restore. Test a put/get with each
consumer after service, proxy or policy changes and before migrating required CI.

Rotate the service GitHub token by replacing its secret file and restarting the
service. Rotate the proxy secret in coordination with the proxy; a mismatch
blocks downloads. Job OIDC tokens are short-lived and need no stored rotation.
Rollback uses the prior pinned runner/service images and retained data snapshot;
do not restore an application workflow that depends on exhausted GitHub artifact
storage as a purported working fallback. No API/schema migration or live
application deployment is part of this rollout.
