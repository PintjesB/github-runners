# CI listener capacity and persistent caches

Each profile keeps one heavy listener and offers a second, optional listener
for `workflow-lint` and `renovate-config`. The heavy labels remain `titan-ci`
and `oportunist-ci`; the light labels are `titan-ci-light` and
`oportunist-ci-light`. Each light listener is limited to one CPU and 2 GiB RAM.
It has its own registration identity and state/work/cache/Codex volumes. Titan
also isolates its browser volume. No login is required for the light listener.
Both listeners have host Docker socket access, including substantial host
privileges, so they are for the same trusted repository as their heavy listener.

## Adoption order

These changes require a newly published runner image containing the updated
`start-runner.sh`. Existing image tags in examples and deployment manifests
are not evidence that this change has been released.

1. Publish and verify each affected runner image through its existing release
   process. Select that real release/digest in the host's deployment settings.
2. Apply the corresponding Compose manifest. Recreate the heavy `runner`
   outside an active job so startup creates owned pip/npm cache directories.
3. Obtain a fresh repository registration token for the new listener and set
   `TITAN_RUNNER_LIGHT_TOKEN` or `OPORTUNIST_RUNNER_LIGHT_TOKEN` in the host's
   mode-0600 env file. Do not reuse the heavy listener's state volume or name.
4. From the appropriate profile directory, explicitly start only the new service:

   ```bash
   docker compose --env-file .env --profile ci-light up -d runner-light
   ```

5. Verify the separate listener is online in GitHub with only its light capability
   label. Blank its short-lived token and recreate that service using the same
   command with `--force-recreate`. Its named state volume preserves identity.
6. Merge the consuming repository's workflow routing changes only after the
   light listener is online. Earlier merging leaves these checks queued.

Ordinary `docker compose up -d` keeps the optional profile disabled and does not
attempt to register a new listener. Titan's `deploy.sh` uses stack-wide lifecycle
commands and orphan handling: drain both listeners before `deploy.sh up` or
`deploy.sh down`. Afterward, explicitly start the `ci-light` profile again and
verify both listeners are online before resuming queued jobs. Use the explicit
Compose command for routine light-service recreation. In the
separate docker-compose repository, activate the same `ci-light` profile and use
its manifest's env variable names. This runbook does not deploy any services.

Titan's light listener disables the pre/post-job hooks. The current post-job
cleanup targets Titan Playwright projects on the shared daemon; running it
after a light job would interfere with the active heavy job. Keep only one heavy
Titan listener until that cleanup and other global resource names are redesigned.

## Package cache maintenance

Each listener mounts its own cache volume at `/var/lib/<profile>-runner/cache`.
`PIP_CACHE_DIR` selects `pip/` and `npm_config_cache` selects `npm/`; startup creates
these directories owned by the runner without walking unrelated state. These
volumes store downloaded packages, never installed environments or node_modules.
The consuming workflows disable both explicit GitHub caches and setup-node's
automatic package-manager cache. Hash-locked Titan installs and `npm ci` remain
unchanged. The first run is cold; later installs reuse valid package data.

Once a week, check each listener's package-cache size:

```bash
docker compose exec --user runner runner bash -c 'du -sh "$PIP_CACHE_DIR" "$npm_config_cache"'
docker compose --profile ci-light exec --user runner runner-light bash -c 'du -sh "$PIP_CACHE_DIR" "$npm_config_cache"'
```

Use 5 GiB total per listener as an operational threshold. If exceeded, drain
that specific listener in GitHub, wait until its job has finished, and stop it.
Then clear only its replaceable package caches with a temporary container:

```bash
docker compose stop runner
docker compose run --rm --no-deps --user runner --entrypoint bash runner -c 'python3 -m pip cache purge && npm cache clean --force'
docker compose up -d runner
```

For the light listener, add `--profile ci-light` and replace the service name
`runner` with `runner-light` in those commands. This is manual maintenance, not
a hard quota or automatic job hook. It avoids deleting in-use cache files,
registration state, workspaces, browsers, and other Docker resources.

## Titan BuildKit state

The Titan application workflows use names `titan-<runner.name>-ci`,
`titan-<runner.name>-publish`, and `titan-<runner.name>-certification`. Names remain
stable across jobs, so interrupted jobs cannot accumulate random builders.
Validation and publication use separate BuildKit workers/state volumes.
Normal setup-action cleanup removes the builder container while retaining its
state for the next job (`keep-state: true`). Each worker enables GC with a
2 GB reserve, 10 GB maximum used-space target, and 5 GB free-space target.
These are GC targets for reclaimable build cache, not hard filesystem quotas;
active build data may temporarily exceed them. Existing `type=gha` image-cache
imports/exports stay enabled until a runner benchmark supports removing them.

Inspect legacy random builders with `docker buildx ls` and BuildKit containers
with `docker ps -a`. Remove a specific confirmed orphan by its exact builder or
container name only after verifying that no runner is using it. Never run a
global Docker prune on this shared daemon. Old orphan builders are not removed
automatically by this change.

Rollback the consuming workflow routing before taking light listeners offline.
Rollback dependency-cache use by restoring workflow cache settings if necessary;
the named package caches are disposable and can remain mounted. Preserve all
registration/state/work volumes throughout rollback.
