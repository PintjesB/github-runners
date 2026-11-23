#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED: clean up after success, failure or cancel.
# Never fails the job, even if the cleanup script is missing.
bash "$(dirname -- "${BASH_SOURCE[0]}")/runner-job-cleanup.sh" completed || true
exit 0
