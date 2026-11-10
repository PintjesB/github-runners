"""Fail-closed tests for upstream GitHub Actions runner release selection."""

import unittest

from scripts.sync_upstream import plan, pins_from_dockerfile


DOCKERFILE = """FROM ubuntu:24.04
ARG RUNNER_VERSION=2.338.2
ARG RUNNER_SHA256_ARM64=670adb2f268fe21dcbb58b3db0e0aa9d5b3bddfe2c651481336e561ff86de945
ARG RUNNER_SHA256_X64=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
"""


def release(version="2.338.3", arm64="a" * 64, x64="b" * 64):
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": f"actions-runner-linux-arm64-{version}.tar.gz", "digest": f"sha256:{arm64}"},
            {"name": f"actions-runner-linux-x64-{version}.tar.gz", "digest": f"sha256:{x64}"},
        ],
    }


class RunnerReleaseUpdateTests(unittest.TestCase):
    def test_atomic_version_and_both_checksums(self):
        updated, version = plan(DOCKERFILE, release())
        self.assertEqual(version, "2.338.3")
        self.assertEqual(pins_from_dockerfile(updated), {
            "RUNNER_VERSION": "2.338.3",
            "RUNNER_SHA256_ARM64": "a" * 64,
            "RUNNER_SHA256_X64": "b" * 64,
        })

    def test_no_downgrade_or_rewrite(self):
        for version in ("2.338.2", "2.338.1"):
            self.assertEqual(plan(DOCKERFILE, release(version)), (DOCKERFILE, None))

    def test_major_upgrade_requires_review(self):
        with self.assertRaisesRegex(ValueError, "major version"):
            plan(DOCKERFILE, release("3.0.0"))

    def test_missing_architecture_is_rejected_without_change(self):
        candidate = release()
        candidate["assets"] = candidate["assets"][:1]
        with self.assertRaisesRegex(ValueError, "x64"):
            plan(DOCKERFILE, candidate)

    def test_digest_must_come_from_official_release_metadata(self):
        candidate = release()
        candidate["assets"][0]["digest"] = "unverified"
        with self.assertRaisesRegex(ValueError, "SHA256"):
            plan(DOCKERFILE, candidate)

    def test_only_stable_semver(self):
        for bad in ("v2.338.3-rc1", "2.338.3", "vlatest"):
            with self.assertRaisesRegex(ValueError, "tag"):
                plan(DOCKERFILE, release(bad.removeprefix("v")) | {"tag_name": bad})
        with self.assertRaisesRegex(ValueError, "prerelease"):
            plan(DOCKERFILE, release() | {"prerelease": True})

    def test_duplicate_or_missing_pins_fail_closed(self):
        for content in (DOCKERFILE.replace("ARG RUNNER_VERSION=2.338.2\n", ""), DOCKERFILE * 2):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                pins_from_dockerfile(content)


if __name__ == "__main__":
    unittest.main()
