"""Exercise persistent-cache startup and resolve isolated light-listener mounts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROFILES = Path(__file__).resolve().parents[2]


class DependencyCacheTests(unittest.TestCase):
    def test_startup_preserves_cache_and_exports_paths_to_listener(self):
        # Privileged ownership/registration operations are unavailable on the
        # developer host. Keep filesystem work and the final listener real.
        for profile in ("titan", "oportunist"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ("state", "image", "cache/pip", "cache/npm"):
                    (root / name).mkdir(parents=True)
                for name in (".runner", ".credentials"):
                    (root / "state" / name).write_text("synthetic")
                (root / "cache/pip/previous").write_text("cached wheel")
                (root / "cache/npm/previous").write_text("cached tarball")
                listener = root / "image/run.sh"
                listener.write_text(
                    '#!/usr/bin/env bash\nset -eu\n'
                    'test -d "$PIP_CACHE_DIR"\ntest -d "$npm_config_cache"\n'
                    'printf "%s\\n" "$PIP_CACHE_DIR" "$npm_config_cache" > "$CACHE_RESULT"\n'
                    'printf warm > "$PIP_CACHE_DIR/new"\n'
                    'printf warm > "$npm_config_cache/new"\n'
                )
                listener.chmod(0o755)
                (root / "bin").mkdir()
                gosu = root / "bin/gosu"
                gosu.write_text('#!/usr/bin/env bash\nshift\nexec "$@"\n')
                gosu.chmod(0o755)
                env = {**os.environ, "RUNNER_STATE_DIR": str(root / "state"),
                       "RUNNER_RUNTIME_DIR": str(root / "runtime"),
                       "RUNNER_WORK_DIR": str(root / "work"),
                       "RUNNER_BROWSER_DIR": str(root / "browser"),
                       "RUNNER_BROWSER_SEED": str(root / "absent-seed"),
                       "RUNNER_ROOT": str(root / "image"),
                       "CODEX_HOME": str(root / "codex"),
                       "CACHE_ROOT": str(root / "cache"),
                       "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
                       "DOCKER_SOCKET": str(root / "absent-socket"),
                       "CACHE_RESULT": str(root / "result"),
                       "OWNERSHIP_RESULT": str(root / "ownership")}
                harness = r'''
                    id() { if [ "$1" = -u ]; then echo 0; else command id "$@"; fi; }
                    function /usr/local/bin/register() { :; }
                    install() {
                        printf '%s\n' "$@" >> "$OWNERSHIP_RESULT"
                        shift 7
                        mkdir -p "$@"
                    }
                    chown() { :; }
                    PIP_CACHE_DIR="$CACHE_ROOT/pip"
                    npm_config_cache="$CACHE_ROOT/npm"
                    source "$1"
                '''
                for _ in range(2):
                    result = subprocess.run(
                        ["bash", "-c", harness, "test-startup",
                         str(PROFILES / profile / "scripts/start-runner.sh")],
                        env=env, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    self.assertEqual((root / "result").read_text().splitlines(),
                                     [str(root / "cache/pip"), str(root / "cache/npm")])
                ownership = (root / "ownership").read_text().splitlines()
                self.assertIn(str(root / "cache/pip"), ownership)
                self.assertIn(str(root / "cache/npm"), ownership)
                self.assertEqual((root / "cache/pip/previous").read_text(), "cached wheel")
                self.assertEqual((root / "cache/npm/previous").read_text(), "cached tarball")

    def test_light_listener_cannot_share_heavy_listener_state_or_cache(self):
        for profile in ("titan", "oportunist"):
            with self.subTest(profile=profile):
                result = subprocess.run(
                    ["docker", "compose", "--env-file", ".env.example",
                     "--profile", "ci-light", "config", "--format", "json"],
                    cwd=PROFILES / profile, check=True, capture_output=True, text=True,
                    env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                         "TITAN_RUNNER_TOKEN": "synthetic-heavy",
                         "TITAN_RUNNER_LIGHT_TOKEN": "synthetic-light",
                         "OPORTUNIST_RUNNER_TOKEN": "synthetic-heavy",
                         "OPORTUNIST_RUNNER_LIGHT_TOKEN": "synthetic-light"},
                )
                model = json.loads(result.stdout)
                heavy = model["services"]["runner"]
                self.assertIn("runner-light", model["services"])
                light = model["services"]["runner-light"]
                self.assertNotEqual(heavy["environment"]["RUNNER_NAME"],
                                    light["environment"]["RUNNER_NAME"])
                self.assertEqual(light["environment"]["RUNNER_LABELS"], f"{profile}-ci-light")
                self.assertEqual(light["environment"]["RUNNER_TOKEN"], "synthetic-light")
                self.assertEqual(heavy["environment"]["RUNNER_TOKEN"], "synthetic-heavy")
                self.assertLessEqual(float(light["cpus"]), 1)
                self.assertLessEqual(int(light["mem_limit"]), 2147483648)
                heavy_sources = {m["source"] for m in heavy["volumes"] if m["type"] == "volume"}
                light_sources = {m["source"] for m in light["volumes"] if m["type"] == "volume"}
                self.assertTrue(heavy_sources.isdisjoint(light_sources))
                for service in (heavy, light):
                    target = f"/var/lib/{profile}-runner/cache"
                    self.assertTrue(any(m["target"] == target and m["type"] == "volume"
                                        for m in service["volumes"]))
                    self.assertEqual(service["environment"]["PIP_CACHE_DIR"], f"{target}/pip")
                    self.assertEqual(service["environment"]["npm_config_cache"], f"{target}/npm")
                if profile == "titan":
                    self.assertFalse(light["environment"].get("ACTIONS_RUNNER_HOOK_JOB_COMPLETED"))
                default = subprocess.run(
                    ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
                    cwd=PROFILES / profile, check=True, capture_output=True, text=True,
                    env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
                )
                self.assertEqual(set(json.loads(default.stdout)["services"]), {"runner"})


if __name__ == "__main__":
    unittest.main()
