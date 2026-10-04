"""Run the startup seeder against real persistent-cache upgrade fixtures."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


STARTUP = Path(__file__).resolve().parents[1] / "scripts/start-runner.sh"


class BrowserCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed, self.dest = self.root / "image-cache", self.root / "persistent-cache"
        self.seed.mkdir()
        self.dest.mkdir()

    def browser(self, root, name, content):
        directory = root / name
        directory.mkdir()
        (directory / "browser").write_text(content)
        (directory / "browser").chmod(0o755)
        (directory / "INSTALLATION_COMPLETE").touch()
        return directory

    def run_seed(self, extra_path=None):
        # Execute the production function without privileged registration and
        # listener startup. All copy/publication filesystem operations stay real.
        source = STARTUP.read_text()
        function = source[source.index("seed_browser_cache() {"):
                          source.index('\nseed_browser_cache "$RUNNER_BROWSER_SEED"')]
        script = 'set -euo pipefail\nlog() { printf "%s\\n" "$*"; }\n' + function
        script += '\nseed_browser_cache "$1" "$2"\n'
        env = {**os.environ}
        if extra_path:
            env["PATH"] = f"{extra_path}:{env['PATH']}"
        return subprocess.run(["bash", "-c", script, "seed-test", str(self.seed), str(self.dest)],
                              env=env, capture_output=True, text=True)

    def test_upgrade_adds_chromium_and_headless_without_replacing_old_cache(self):
        self.browser(self.dest, "chromium-1228", "old Chromium")
        self.browser(self.dest, "chromium_headless_shell-1228", "old headless")
        self.browser(self.dest, "ffmpeg-1011", "existing ffmpeg")
        self.browser(self.seed, "chromium-1243", "new Chromium")
        self.browser(self.seed, "chromium_headless_shell-1243", "new headless")
        self.browser(self.seed, "ffmpeg-1011", "image ffmpeg")
        (self.dest / ".links").mkdir()
        (self.dest / ".links/frontend").write_text("/synthetic/frontend/playwright-core")
        (self.seed / ".links").mkdir()
        (self.seed / ".links/probe").write_text("/opt/titan-probe/node_modules/playwright-core")

        result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.dest / "chromium-1243/browser").is_file())
        self.assertTrue((self.dest / "chromium_headless_shell-1243/browser").is_file())
        self.assertEqual((self.dest / "chromium-1243/browser").read_text(), "new Chromium")
        self.assertEqual((self.dest / "chromium_headless_shell-1243/browser").read_text(), "new headless")
        self.assertEqual((self.dest / "chromium-1228/browser").read_text(), "old Chromium")
        self.assertEqual((self.dest / "chromium_headless_shell-1228/browser").read_text(), "old headless")
        self.assertEqual((self.dest / "ffmpeg-1011/browser").read_text(), "existing ffmpeg")
        self.assertEqual((self.dest / ".links/frontend").read_text(), "/synthetic/frontend/playwright-core")
        self.assertEqual((self.dest / ".links/probe").read_text(), "/opt/titan-probe/node_modules/playwright-core")

    def test_repeat_start_reuses_complete_revision_without_copying(self):
        self.browser(self.seed, "chromium-1243", "image browser")
        self.browser(self.dest, "chromium-1243", "existing browser")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        cp = fake_bin / "cp"
        cp.write_text("#!/usr/bin/env bash\nexit 99\n")
        cp.chmod(0o755)
        result = self.run_seed(fake_bin)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.dest / "chromium-1243/browser").read_text(), "existing browser")

    def test_missing_image_seed_leaves_persistent_cache_untouched(self):
        self.browser(self.dest, "chromium-1228", "existing browser")
        self.seed.rmdir()
        result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.dest / "chromium-1228/browser").read_text(), "existing browser")

    def test_failed_copy_cannot_publish_partial_revision_and_retry_recovers(self):
        self.browser(self.dest, "chromium-1228", "old browser")
        self.browser(self.seed, "chromium-1243", "new browser")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        cp = fake_bin / "cp"
        cp.write_text(
            '#!/usr/bin/env bash\n'
            'target="${!#}"\nmkdir -p "$target"\n'
            'touch "$target/INSTALLATION_COMPLETE"\nexit 42\n'
        )
        cp.chmod(0o755)
        result = self.run_seed(fake_bin)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dest / "chromium-1243").exists())
        self.assertEqual((self.dest / "chromium-1228/browser").read_text(), "old browser")
        result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.dest / "chromium-1243/browser").read_text(), "new browser")

    def test_existing_incomplete_revision_fails_without_overwriting_user_files(self):
        self.browser(self.seed, "chromium-1243", "new browser")
        partial = self.dest / "chromium-1243"
        partial.mkdir()
        (partial / "user-file").write_text("preserve")
        result = self.run_seed()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((partial / "user-file").read_text(), "preserve")
        self.assertFalse((partial / "INSTALLATION_COMPLETE").exists())


if __name__ == "__main__":
    unittest.main()
