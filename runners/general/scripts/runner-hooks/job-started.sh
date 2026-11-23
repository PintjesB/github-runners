#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_STARTED: recover from an interrupted previous job.
# Never fails the job, even if the cleanup script is missing.
bash "$(dirname -- "${BASH_SOURCE[0]}")/runner-job-cleanup.sh" started || true
exit 0
