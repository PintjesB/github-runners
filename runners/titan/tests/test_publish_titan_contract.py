from __future__ import annotations

from pathlib import Path
import re

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROFILE_ROOT.parents[1]
WRAPPER = REPO_ROOT / ".github" / "workflows" / "publish-titan.yml"
REUSABLE = REPO_ROOT / ".github" / "workflows" / "_publish-runner.yml"
SEMVER = REPO_ROOT / ".github" / "workflows" / "_tag-semver.yml"
VERSION_FILE = PROFILE_ROOT / "VERSION"


def _yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_titan_wrapper_targets_profile_and_existing_image_name() -> None:
    data = _yaml(WRAPPER)
    job = data["jobs"]["publish"]
    assert job["uses"] == "./.github/workflows/_publish-runner.yml"
    assert job["with"]["profile"] == "titan"
    assert job["with"]["image"] == "ghcr.io/pintjesb/titan-stocks-runner"


def test_titan_wrapper_republishes_when_shared_workflow_changes() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert "'.github/workflows/_publish-runner.yml'" in text
    assert "'.github/workflows/_tag-semver.yml'" in text
    assert "'runners/titan/**'" in text


def test_workflows_parse_and_have_no_executable_fixed_host_ports() -> None:
    for path in (WRAPPER, REUSABLE, SEMVER):
        data = _yaml(path)
        assert isinstance(data, dict), f"{path.name} must parse as a YAML mapping"
        assert "jobs" in data, f"{path.name} must declare jobs"
        for job_name, job in data["jobs"].items():
            assert isinstance(job, dict), f"{path.name} job {job_name!r} must be a mapping"
            for index, step in enumerate(job.get("steps", []), start=1):
                assert isinstance(step, dict), (
                    f"{path.name} job {job_name!r} step {index} must be a mapping"
                )
                assert step.get("run") or step.get("uses"), (
                    f"{path.name} job {job_name!r} step {index} must declare run or uses"
                )
                run = step.get("run", "")
                for line in run.splitlines():
                    if line.lstrip().startswith("#"):
                        continue
                    assert "ports:" not in line, (
                        f"{path.name} must not declare a fixed host port binding"
                    )


def test_reusable_publisher_runs_profile_contract_before_build() -> None:
    data = _yaml(REUSABLE)
    jobs = data["jobs"]
    assert jobs["build"]["needs"] == "contract"
    assert "./ci-contract.sh" in REUSABLE.read_text(encoding="utf-8")


def test_reusable_publisher_builds_native_amd64_and_arm64() -> None:
    data = _yaml(REUSABLE)
    matrix = data["jobs"]["build"]["strategy"]["matrix"]["include"]
    pairs = {(entry["platform"], entry["runner"]) for entry in matrix}
    assert pairs == {
        ("linux/amd64", "ubuntu-24.04"),
        ("linux/arm64", "ubuntu-24.04-arm"),
    }
    text = REUSABLE.read_text(encoding="utf-8")
    assert "provenance: false" in text
    assert "sbom: false" in text
    assert "candidate-${{ github.sha }}-${{ matrix.tag_suffix }}" in text


def test_reusable_publisher_uses_immutable_digest_handoff() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert re.search(r"actions/upload-artifact@[0-9a-f]{40}", text)
    assert re.search(r"actions/download-artifact@[0-9a-f]{40}", text)
    assert '"${IMAGE}@${amd64}"' in text
    assert '"${IMAGE}@${arm64}"' in text


def test_reusable_publisher_handles_ambiguous_imagetools_exit() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert "set +e" in text
    assert "create_rc=$?" in text
    assert "imagetools create diagnostics" in text
    assert "merged manifest could not be resolved after imagetools create" in text


def test_reusable_publisher_validates_exact_merged_platforms() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert '("linux", "amd64")' in text
    assert '("linux", "arm64")' in text
    assert "if actual != expected" in text
    assert '"--format", "{{json .Manifest}}"' in text


def test_reusable_publisher_probes_without_host_network_or_ipc() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert "--add-host host.docker.internal:host-gateway" in text
    assert "--shm-size 2gb" in text
    assert "--network host" not in text
    assert "--ipc host" not in text
    assert "-e EXPECTED_ARCH=" in text


def test_reusable_publisher_attests_before_exact_promotion() -> None:
    data = _yaml(REUSABLE)
    jobs = data["jobs"]
    assert jobs["attest"]["needs"] == "verify"
    assert jobs["promote"]["needs"] == "attest"
    attest_uses = [
        step["uses"]
        for step in jobs["attest"]["steps"]
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/attest@")
    ]
    assert len(attest_uses) == 1
    assert re.fullmatch(r"actions/attest@[0-9a-f]{40}", attest_uses[0])
    text = REUSABLE.read_text(encoding="utf-8")
    assert '"${IMAGE}@${merged}"' in text
    assert '[ "$latest" != "$merged" ]' in text


def test_titan_semver_job_runs_only_after_verified_publish() -> None:
    data = _yaml(WRAPPER)
    version_job = data["jobs"]["version"]
    assert version_job["needs"] == "publish"
    assert version_job["uses"] == "./.github/workflows/_tag-semver.yml"
    assert version_job["with"]["profile"] == "titan"
    assert version_job["with"]["image"] == "ghcr.io/pintjesb/titan-stocks-runner"


def test_profile_version_floor_is_valid_semver_core() -> None:
    import re

    value = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)


def test_semver_workflow_uses_ghcr_as_independent_release_ledger() -> None:
    text = SEMVER.read_text(encoding="utf-8")
    assert 'version_file="runners/${PROFILE}/VERSION"' in text
    assert "/packages/container/${encoded_package}/versions?per_page=100" in text
    assert "gh api --paginate" in text
    assert "current_digest_patches" in text
    assert "highest + 1" in text
    assert "max(floor_patch, highest + 1)" in text


def test_semver_tagging_is_idempotent_and_collision_safe() -> None:
    text = SEMVER.read_text(encoding="utf-8")
    assert "Re-running the same successfully published image is idempotent" in text
    assert "version collision:" in text
    assert '"${IMAGE}@${EXPECTED_DIGEST}"' in text
    assert "latest moved during SemVer tagging" in text


def test_digest_extractors_drain_large_inspect_output(tmp_path) -> None:
    """A producer can fail with EPIPE if awk exits after the first header."""
    import re
    import subprocess
    import sys

    producer = tmp_path / 'inspect.py'
    producer.write_text('import os\nos.write(1, b"Digest: sha256:first\\n")\n'
                        'for _ in range(256): os.write(1, b"x" * 65536 + b"\\n")\n'
                        'os.write(1, b"Digest: sha256:child\\n")\n')
    programs = set(re.findall(r"awk '([^']*Digest:[^']*)'", REUSABLE.read_text() + SEMVER.read_text()))
    assert programs
    for program in programs:
        result = subprocess.run(['bash', '-o', 'pipefail', '-c',
                                 '"$1" "$2" | awk "$3"', 'extract', sys.executable,
                                 str(producer), program], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout == 'sha256:first\n'
