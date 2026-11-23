"""Standard-library tests for the built-in runner cleanup hooks."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

PROFILE = Path(__file__).resolve().parents[1]
HOOKS = PROFILE / "scripts" / "runner-hooks"
SHELL = ("bash", "--noprofile", "--norc", "-e", "-o", "pipefail")


def put(path, contents="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return path


class JobCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.work = self.base / "work"
        self.repo = self.work / "innerflows"
        self.workspace = self.repo / "innerflows"
        self.runtime = self.base / "runtime"
        self.diag = self.runtime / "_diag"
        self.cache = self.base / "cache"
        self.tmp = self.base / "tmp"
        self.outside = put(self.base / "outside" / "sentinel", "preserve")
        for path in (self.workspace, self.work / "_temp", self.work / "_actions",
                     self.work / "_tool", self.diag, self.cache / "pip",
                     self.cache / "npm", self.tmp):
            path.mkdir(parents=True, exist_ok=True)
        self.important = [
            put(self.work / "_actions" / "checkout.tar"),
            put(self.work / "_temp" / "command"),
            put(self.runtime / ".runner"),
            put(self.runtime / ".credentials"),
        ]
        self.env = dict(os.environ, RUNNER_WORKSPACE=str(self.repo),
                        GITHUB_WORKSPACE=str(self.workspace),
                        RUNNER_TOOL_CACHE=str(self.work / "_tool"),
                        RUNNER_RUNTIME_DIR=str(self.runtime),
                        RUNNER_CLEANUP_TMP_DIR=str(self.tmp),
                        PIP_CACHE_DIR=str(self.cache / "pip"),
                        npm_config_cache=str(self.cache / "npm"),
                        RUNNER_CLEANUP_MIN_FREE_GB="0",
                        RUNNER_CLEANUP_MIN_FREE_INODES_PCT="0")

    def run_hook(self, phase="completed", **overrides):
        result = subprocess.run([*SHELL, str(HOOKS / f"job-{phase}.sh")],
                                env=dict(self.env, **overrides),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def assert_preserved(self):
        for path in [*self.important, self.outside]:
            self.assertTrue(path.exists(), f"must preserve {path}")

    def test_completion_cleans_workspaces_without_touching_state(self):
        file = put(self.workspace / ".venv" / "bin" / "python")
        other = put(self.work / "another" / "another" / "node_modules" / "pkg")
        self.assertIn("[runner-cleanup:completed]", self.run_hook())
        self.assertFalse(file.exists())
        self.assertFalse(other.exists())
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assert_preserved()

    def test_start_recovers_after_interruption(self):
        file = put(self.workspace / "stale" / "artifact")
        self.assertIn("[runner-cleanup:started]", self.run_hook("started"))
        self.assertFalse(file.exists())
        self.assert_preserved()

    def test_dry_run_and_disable(self):
        file = put(self.workspace / "test.txt")
        self.assertIn("would remove", self.run_hook(RUNNER_CLEANUP_DRY_RUN="1"))
        self.assertTrue(file.exists())
        self.assertIn("disabled", self.run_hook(RUNNER_CLEANUP_DISABLE="1"))
        self.assertTrue(file.exists())

    def test_symlink_escape_cannot_delete_external_files(self):
        (self.workspace / "escape").symlink_to(self.outside.parent, target_is_directory=True)
        file = put(self.workspace / "inside")
        self.run_hook()
        self.assertFalse(file.exists())
        self.assertFalse((self.workspace / "escape").exists())
        self.assertEqual(self.outside.read_text(), "preserve")
        self.assert_preserved()

    def test_rejects_unsafe_work_root(self):
        file = put(self.workspace / "test.txt")
        for overrides in ({"RUNNER_WORKSPACE": "relative"},
                          {"RUNNER_WORKSPACE": "/innerflows"},
                          {"GITHUB_WORKSPACE": str(self.outside)}):
            self.assertIn("nothing cleaned", self.run_hook(**overrides))
            self.assertTrue(file.exists())
        self.assert_preserved()

    def test_cache_limits(self):
        oversized = self.cache / "pip" / "big"
        oversized.write_bytes(b"x" * (2 * 1024 * 1024))
        small = put(self.cache / "npm" / "small")
        self.assertIn("pip cache", self.run_hook(RUNNER_CLEANUP_CACHE_MAX_MB="1"))
        self.assertFalse(oversized.exists())
        self.assertTrue(small.exists())

    def test_default_caches_are_disposable(self):
        pip = put(self.cache / "pip" / "package")
        npm = put(self.cache / "npm" / "package")
        self.run_hook()
        self.assertFalse(pip.exists())
        self.assertFalse(npm.exists())
        self.assert_preserved()

    def test_exported_work_root_works_without_runner_workspace(self):
        artifact = put(self.workspace / "node_modules" / "package")
        self.run_hook(RUNNER_WORKSPACE="", RUNNER_WORK_DIR=str(self.work))
        self.assertFalse(artifact.exists())
        self.assert_preserved()

    def test_started_hook_gates_low_disk_without_dropping_state(self):
        artifact = put(self.workspace / "stale")
        env = dict(self.env, RUNNER_CLEANUP_MIN_FREE_GB="1024")
        result = subprocess.run(
            [*SHELL, str(HOOKS / "job-started.sh")],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("insufficient disk space", result.stdout)
        self.assertFalse(artifact.exists())
        self.assert_preserved()

    def test_startup_cleanup_does_not_require_job_environment(self):
        artifact = put(self.workspace / "stale")
        env = dict(self.env, RUNNER_WORKSPACE="",
                   GITHUB_WORKSPACE="", RUNNER_WORK_DIR=str(self.work))
        temp_file = self.work / "_temp" / "command"
        temp_file.unlink()
        (self.work / "_temp").rmdir()
        self.important.remove(temp_file)
        result = subprocess.run(
            [*SHELL, str(HOOKS / "runner-job-cleanup.sh"), "startup"],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(artifact.exists())
        self.assert_preserved()

    def test_temp_retention(self):
        old_dir = self.tmp / "old"
        old = put(old_dir / "temp")
        os.utime(old_dir, (time.time() - 86400,) * 2)
        recent = put(self.tmp / "new")
        self.run_hook()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_diag_cleans_old_worker_keeps_live_listener(self):
        old = put(self.diag / "Worker_old.log")
        os.utime(old, (time.time() - 86400 * 10,) * 2)
        current = put(self.diag / "Runner_current.log")
        self.run_hook()
        self.assertFalse(old.exists())
        self.assertTrue(current.exists())
        self.assert_preserved()

    def test_missing_cleanup_script_never_fails_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "job-completed.sh"
            shutil.copyfile(HOOKS / "job-completed.sh", copy)
            result = subprocess.run([*SHELL, str(copy)], env=self.env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_untrusted_cache_locations_cannot_remove_registration_or_actions(self):
        # A workflow may set cache variables; none can redirect cleanup
        # to runner state, credentials or the Actions cache.
        for target in (self.runtime, self.work / "_actions", self.work / "_temp"):
            protected = put(target / "protected", "do-not-delete")
            self.run_hook(PIP_CACHE_DIR=str(target),
                          npm_config_cache=str(target),
                          RUNNER_TOOL_CACHE=str(target),
                          RUNNER_CLEANUP_TOOLCACHE_MAX_MB="0",
                          RUNNER_CLEANUP_CACHE_MAX_MB="0")
            self.assertTrue(protected.exists(), str(target))
        self.assert_preserved()

    def test_untrusted_temp_and_diag_cannot_target_runtime(self):
        protected = put(self.runtime / "Runner_sensitive.log", "preserve")
        os.utime(protected, (time.time() - 86400 * 12,) * 2)
        self.run_hook(RUNNER_CLEANUP_TMP_DIR=str(self.runtime),
                      RUNNER_CLEANUP_DIAG_DIR=str(self.runtime),
                      RUNNER_CLEANUP_TMP_MINUTES="0",
                      RUNNER_CLEANUP_DIAG_DAYS="0")
        self.assertTrue(protected.exists())
        self.assert_preserved()

    def test_mount_under_workspace_is_not_traversed(self):
        # Simulate a same-filesystem bind mount with a fake findmnt output.
        # The cleanup must skip the entire parent rather than chmod it.
        nested = self.workspace / "mounted" / "bind"
        file = put(nested / "keep", "mount-data")
        cmd_dir = self.base / "bin"
        script = put(cmd_dir / "findmnt",
                     "#!/usr/bin/env bash\n"
                     + "printf '%s\\n' " + repr(str(nested)) + "\n")
        script.chmod(0o755)
        log = self.run_hook(PATH=str(cmd_dir) + ":" + os.environ["PATH"])
        self.assertIn("skip mounted workspace:", log)
        self.assertTrue(file.exists())
        self.assert_preserved()

    def test_runtime_home_caches_are_cleared_without_touching_credentials(self):
        paths = [
            put(self.runtime / ".cache" / "uv" / "package"),
            put(self.runtime / ".npm" / "_cacache" / "content"),
            put(self.runtime / ".local" / "share" / "pnpm" / "store" / "package"),
        ]
        self.run_hook()
        for path in paths:
            self.assertFalse(path.exists(), str(path))
        self.assert_preserved()

    def test_runtime_cache_symlink_parent_cannot_escape(self):
        other = put(self.base / "other" / "share" / "pnpm" / "store" / "secret")
        (self.runtime / ".local").symlink_to(other.parents[3], target_is_directory=True)
        log = self.run_hook()
        self.assertIn("unsafe or mounted runtime cache", log)
        self.assertTrue(other.exists())
        self.assert_preserved()

    def test_started_hook_rejects_mounted_workspace_without_wiping_mount(self):
        nested = self.workspace / "mount" / "data"
        preserved = put(nested / "important")
        cmd_dir = self.base / "bin"
        script = put(cmd_dir / "findmnt",
                     "#!/usr/bin/env bash\n"
                     + "printf '%s\\n' " + repr(str(nested)) + "\n")
        script.chmod(0o755)
        env = dict(self.env, PATH=str(cmd_dir) + ":" + os.environ["PATH"])
        result = subprocess.run(
            [*SHELL, str(HOOKS / "job-started.sh")],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("skip mounted workspace", result.stdout)
        self.assertIn("refusing new job", result.stdout)
        self.assertTrue(preserved.exists())
        self.assert_preserved()

    def test_started_hook_rejects_unusable_package_cache(self):
        (self.cache / "npm").rmdir()
        result = subprocess.run(
            [*SHELL, str(HOOKS / "job-started.sh")],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or not ours", result.stdout)
        self.assertIn("refusing new job", result.stdout)
        self.assert_preserved()

    def test_started_hook_rejects_unmeasurable_cache(self):
        cmd_dir = self.base / "bin"
        script = put(cmd_dir / "du", "#!/usr/bin/env bash\nexit 1\n")
        script.chmod(0o755)
        env = dict(self.env, PATH=str(cmd_dir) + ":" + os.environ["PATH"])
        result = subprocess.run(
            [*SHELL, str(HOOKS / "job-started.sh")],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unable to measure", result.stdout)
        self.assertIn("refusing new job", result.stdout)
        self.assert_preserved()

    def test_hooks_have_bounded_execution(self):
        started = (HOOKS / "job-started.sh").read_text()
        completed = (HOOKS / "job-completed.sh").read_text()
        startup = (PROFILE / "scripts" / "start-runner.sh").read_text()
        for script in (started, completed, startup):
            self.assertIn("timeout --signal=TERM --kill-after=10s 300s", script)
        self.assertIn("exit 0", completed)

    def test_started_hook_blocks_symlinked_workspace_parent(self):
        link = self.work / "linked-repo"
        link.symlink_to(self.outside.parent, target_is_directory=True)
        result = subprocess.run(
            [*SHELL, str(HOOKS / "job-started.sh")],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked workspace parent", result.stdout)
        self.assertTrue(link.is_symlink())
        self.assert_preserved()

    def test_dockerfile_contract(self):
        dockerfile = (PROFILE / "Dockerfile").read_text()
        self.assertIn("COPY --chown=root:root --chmod=0755 scripts/runner-hooks/", dockerfile)
        self.assertIn("ENV ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/runner-hooks/job-started.sh", dockerfile)
        self.assertNotIn("runner-hooks:", (PROFILE / "docker-compose.yml").read_text())


if __name__ == "__main__":
    unittest.main()
