#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_STARTED: recover from an interrupted previous job.
# Fail the assigned job if cleanup fails or the disk/inode gate is closed.
# The listener remains registered and starts no workflow steps for this job.
# GitHub has no native hook timeout. Bound cleanup so a broken filesystem or
# path cannot stall this runner indefinitely; timeout fails this job closed.
# Reclaim any PostgreSQL cluster left by a cancelled or killed prior job.
# A failed stop blocks new work so the old server cannot leak across jobs.
if [ -x /usr/local/bin/runner-ci-postgres ]; then
    timeout --signal=TERM --kill-after=10s 60s /usr/local/bin/runner-ci-postgres stop || exit $?
fi
exec timeout --signal=TERM --kill-after=10s 300s bash "$(dirname -- "${BASH_SOURCE[0]}")/runner-job-cleanup.sh" started
