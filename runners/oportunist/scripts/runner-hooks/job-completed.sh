#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED: clean up after success, failure or cancel.
# Never fails the job, even if the cleanup script is missing.
# Completion must never override the job's test result; a timed-out cleanup
# is logged and retried before the next job.
rc=0
timeout --signal=TERM --kill-after=10s 300s bash "$(dirname -- "${BASH_SOURCE[0]}")/runner-job-cleanup.sh" completed || rc=$?
if [ "$rc" -ne 0 ]; then
    echo "[runner-cleanup:completed] WARNING: cleanup failed or timed out (exit $rc); next job retries" >&2
fi
exit 0
