"""Contracts for the built-in on-demand PostgreSQL 16 runner lifecycle."""
from pathlib import Path
import unittest

PROFILE = Path(__file__).resolve().parents[1]
SCRIPT = (PROFILE / "scripts" / "runner-ci-postgres.sh").read_text()
DOCKERFILE = (PROFILE / "Dockerfile").read_text()
PROBE = (PROFILE / "scripts" / "probe.sh").read_text()
STARTED = (PROFILE / "scripts" / "runner-hooks" / "job-started.sh").read_text()
COMPLETED = (PROFILE / "scripts" / "runner-hooks" / "job-completed.sh").read_text()
ENTRYPOINT = (PROFILE / "scripts" / "start-runner.sh").read_text()


class PostgresRunnerTests(unittest.TestCase):
    def test_server_version_baked_without_persistent_cluster(self):
        self.assertIn("PG_MAJOR=16", DOCKERFILE)
        self.assertIn("postgresql-16 postgresql-client-16", DOCKERFILE)
        self.assertIn("create_main_cluster = false", DOCKERFILE)
        self.assertIn("test ! -d /var/lib/postgresql/16/main", DOCKERFILE)
        self.assertIn("runner-ci-postgres.sh /usr/local/bin/runner-ci-postgres", DOCKERFILE)

    def test_no_docker_service_or_registration_change(self):
        for forbidden in ("docker run", "docker compose", "docker volume",
                          ".credentials", ".runner", "RUNNER_TOKEN"):
            self.assertNotIn(forbidden, SCRIPT)
        self.assertIn("RUNNER_WORK_DIR", SCRIPT)
        self.assertIn('BASE="$WORK/_ci_postgres"', SCRIPT)

    def test_per_job_auth_and_loopback_only(self):
        self.assertIn("--auth-host=scram-sha-256", SCRIPT)
        self.assertIn("--auth-local=reject", SCRIPT)
        self.assertIn("listen_addresses=127.0.0.1", SCRIPT)
        self.assertIn("unix_socket_directories=", SCRIPT)
        self.assertIn("secrets.token_hex(24)", SCRIPT)
        self.assertIn("GITHUB_ENV", SCRIPT)
        self.assertIn("POSTGRES_TEST_URL", SCRIPT)
        self.assertIn("CI_POSTGRES_URL", SCRIPT)
        self.assertIn("SHOW listen_addresses", SCRIPT)

    def test_lifecycle_is_scoped_and_idempotent(self):
        self.assertIn("start|stop|status", SCRIPT)
        self.assertIn('stop_locked', SCRIPT)
        self.assertIn("flock -x 9", SCRIPT)
        # The forked PostgreSQL daemon must not inherit the held flock FD.
        self.assertIn("start 9>&-", SCRIPT)
        self.assertIn("realpath -e", SCRIPT)
        self.assertIn("check_mounts", SCRIPT)
        self.assertIn("rm -rf --one-file-system --", SCRIPT)
        self.assertIn('refusing to remove data', SCRIPT)
        self.assertIn('trap on_exit EXIT', SCRIPT)

    def test_all_hooks_stop_postgres_even_if_cache_cleanup_disabled(self):
        self.assertIn("runner-ci-postgres stop", STARTED)
        self.assertIn("runner-ci-postgres stop", COMPLETED)
        self.assertIn("runner-ci-postgres stop", ENTRYPOINT)
        self.assertLess(STARTED.index("runner-ci-postgres stop"),
                        STARTED.index("runner-job-cleanup.sh"))
        self.assertLess(COMPLETED.index("runner-ci-postgres stop"),
                        COMPLETED.index("runner-job-cleanup.sh"))
        self.assertLess(ENTRYPOINT.index("runner-ci-postgres stop"),
                        ENTRYPOINT.index("runner-job-cleanup.sh"))
        self.assertIn("exit 0", COMPLETED)

    def test_native_probe_executes_sql_and_removes_pgdata(self):
        self.assertIn("gosu runner /usr/local/bin/runner-ci-postgres start", PROBE)
        self.assertIn('select 1', PROBE)
        self.assertIn("gosu runner /usr/local/bin/runner-ci-postgres stop", PROBE)
        self.assertIn("PostgreSQL PGDATA survived cleanup", PROBE)
        # Live native probes must exercise the actual baked hooks with a
        # running server, not merely grep their source for stop invocations.
        self.assertIn("gosu runner /opt/runner-hooks/job-completed.sh", PROBE)
        self.assertIn("completed hook left PostgreSQL PGDATA", PROBE)
        self.assertIn("gosu runner /opt/runner-hooks/job-started.sh", PROBE)
        self.assertIn("started hook left PostgreSQL PGDATA", PROBE)


if __name__ == "__main__":
    unittest.main()
