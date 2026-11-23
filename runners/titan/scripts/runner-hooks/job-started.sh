#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_STARTED: recover from an interrupted previous job.
# Fail the assigned job if cleanup fails or the disk/inode gate is closed.
# The listener remains registered and starts no workflow steps for this job.
# GitHub has no native hook timeout. Bound cleanup so a broken filesystem or
# path cannot stall this runner indefinitely; timeout fails this job closed.
exec timeout --signal=TERM --kill-after=10s 300s bash "$(dirname -- "${BASH_SOURCE[0]}")/runner-job-cleanup.sh" started
