from __future__ import annotations

import hashlib
import importlib.util
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "evidence" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("evidence_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)
    return module


@pytest.fixture
def service(tmp_path: Path):
    module = load_server(); root = tmp_path / "store"; root.mkdir()
    (root / ".titan-evidence-store-v1").write_text("provisioned\n")
    policy = {"repositories": {"PintjesB/titan-stocks": {
        "repository_id": "123", "owner_id": "456", "trusted_runner_label": "titan-ci",
        "workflows": {".github/workflows/ci.yml": {
            "required-ci": {"retention_days": 14, "jobs": {"required-ci": "Required CI"}},
            "full-certification": {"retention_days": 30, "jobs": {"certify": "Full certification"}},
            "live-provider": {"retention_days": 30, "jobs": {"live": "Live provider"}},
            "runtime-sbom": {"retention_days": 90, "jobs": {"sbom": "Runtime SBOM"}},
        }}}}}
    claims = {"repository": "PintjesB/titan-stocks", "repository_id": "123", "repository_owner_id": "456",
              "sha": "a" * 40, "run_id": "12", "run_attempt": "3",
              "workflow_ref": "PintjesB/titan-stocks/.github/workflows/ci.yml@refs/heads/master",
              "actor": "trusted", "event_name": "push", "check_run_id": "111"}
    metadata = {"head_repository": "PintjesB/titan-stocks", "jobs": [{"name": "Required CI", "labels": ["self-hosted", "titan-ci"], "check_run_id": "111"}]}
    instance = module.EvidenceService(root, policy, lambda _token: claims, lambda *_args: metadata,
                                      download_base="https://evidence.test/downloads", require_mount=False)
    instance.error_type = module.EvidenceError
    return instance


def key(profile: str = "required-ci", job: str = "required-ci") -> str:
    return f"PintjesB/titan-stocks/{'a' * 40}/12/3/{job}/{profile}.tar.gz"


def test_store_retry_conflict_and_run_scoped_read(service) -> None:
    receipt = service.put(key(), b"abc", 14, "jwt")
    assert receipt == {"version": 1, "key": key(), "sha256": hashlib.sha256(b"abc").hexdigest(),
                       "size": 3, "url": f"https://evidence.test/downloads/{key()}"}
    assert service.put(key(), b"abc", 14, "jwt") == receipt
    with pytest.raises(service.error_type) as conflict: service.put(key(), b"different", 14, "jwt")
    assert conflict.value.status == 409
    assert service.get(key(), "jwt") == b"abc"


@pytest.mark.parametrize("bad_key", ["../x", "/absolute", "PintjesB/titan-stocks/nope", "PintjesB/other/" + "a" * 40 + "/12/3/job/required-ci.tar.gz"])
def test_malformed_and_wrong_repository_keys_are_rejected(service, bad_key: str) -> None:
    with pytest.raises(service.error_type): service.put(bad_key, b"abc", 14, "jwt")


def test_profile_retention_job_and_metadata_are_fail_closed(service) -> None:
    for bad_key, days in [(key("required-ci"), 30), (key("unknown"), 14), (key(job="other"), 14)]:
        with pytest.raises(service.error_type): service.put(bad_key, b"abc", days, "jwt")
    service.metadata_resolver = lambda *_args: (_ for _ in ()).throw(OSError("offline"))
    with pytest.raises(service.error_type) as unavailable: service.put(key(), b"abc", 14, "jwt")
    assert unavailable.value.status == 503


def test_symlink_store_component_and_missing_marker_are_rejected(tmp_path: Path, service) -> None:
    outside = tmp_path / "outside"; outside.mkdir(); owner = service.root / "PintjesB"
    owner.symlink_to(outside, target_is_directory=True)
    with pytest.raises(service.error_type): service.put(key(), b"abc", 14, "jwt")
    owner.unlink(); (service.root / ".titan-evidence-store-v1").unlink()
    with pytest.raises(service.error_type): service.ensure_ready()


def test_concurrent_same_key_writes_publish_one_complete_object(service) -> None:
    results, failures = [], []
    def write() -> None:
        try: results.append(service.put(key(), b"x" * 1024, 14, "jwt"))
        except Exception as exc: failures.append(exc)
    threads = [threading.Thread(target=write) for _ in range(8)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert not failures
    assert len(results) == 8 and len({item["sha256"] for item in results}) == 1
    assert service.get(key(), "jwt") == b"x" * 1024


def test_claim_binding_fork_dependabot_and_other_attempt_are_rejected(service) -> None:
    original = service.token_verifier
    def claims(**overrides): value = dict(original("jwt")); value.update(overrides); return value
    for overrides in ({"run_attempt": "4"}, {"repository_id": "999"}, {"event_name": "dynamic", "actor": "dependabot[bot]"}):
        service.token_verifier = lambda _token, o=overrides: claims(**o)
        with pytest.raises(service.error_type): service.put(key(), b"abc", 14, "jwt")
    service.token_verifier = original
    service.metadata_resolver = lambda *_args: {"head_repository": "attacker/fork", "jobs": [{"name": "Required CI", "labels": ["titan-ci"]}]}
    with pytest.raises(service.error_type): service.put(key(), b"abc", 14, "jwt")


def test_upload_limit_and_partial_failure_leave_no_visible_object(service) -> None:
    service.max_bytes = 2
    with pytest.raises(service.error_type): service.put(key(), b"abc", 14, "jwt")
    assert not service.object_path(key()).exists()
    assert list((service.root / ".staging").iterdir()) == []



def test_cannot_impersonate_another_job_in_the_same_run(service):
    original = service.metadata_resolver
    metadata = original()
    metadata['jobs'].append({'name': 'Runtime SBOM', 'labels': ['self-hosted', 'titan-ci'], 'check_run_id': '222'})
    service.metadata_resolver = lambda *_: metadata
    with pytest.raises(service.error_type):
        service.put(key('runtime-sbom', 'sbom'), b'forged', 90, 'jwt')


def test_read_rejects_symlinked_parent(service, tmp_path):
    service.put(key(), b'original', 14, 'jwt')
    owner = service.root / 'PintjesB'
    outside = tmp_path / 'outside'
    owner.rename(outside)
    owner.symlink_to(outside, target_is_directory=True)
    with pytest.raises(service.error_type):
        service.get(key(), 'jwt')


def test_signed_jwt_verifies_signature_audience_and_expiry(tmp_path, service):
    import json
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    module = load_server()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk['kid'] = 'fixture-key'
    keys = tmp_path / 'jwks.json'
    keys.write_text(json.dumps({'keys': [jwk]}))
    verifier = module.JwtVerifier(issuer='https://token.actions.githubusercontent.com', audience='evidence-test', jwks_file=keys)
    claims = {**service.token_verifier(''), 'iss': 'https://token.actions.githubusercontent.com',
              'aud': 'evidence-test', 'sub': 'repo:PintjesB/titan-stocks:ref:refs/heads/master',
              'iat': int(time.time()), 'nbf': int(time.time()), 'exp': int(time.time()) + 60}
    token = jwt.encode(claims, private, algorithm='RS256', headers={'kid': 'fixture-key'})
    assert verifier(token)['run_id'] == '12'
    for patch in ({'aud': 'wrong'}, {'exp': int(time.time()) - 60}, {'iss': 'https://attacker.invalid'}):
        invalid = jwt.encode({**claims, **patch}, private, algorithm='RS256', headers={'kid': 'fixture-key'})
        with pytest.raises(module.EvidenceError):
            verifier(invalid)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(module.EvidenceError):
        verifier(jwt.encode(claims, other, algorithm='RS256', headers={'kid': 'fixture-key'}))


@pytest.mark.parametrize('human', [False, True])
def test_download_rejects_same_size_corruption(service, human):
    service.put(key(), b'abc', 14, 'jwt')
    service.object_path(key()).write_bytes(b'xyz')
    with pytest.raises(service.error_type) as error:
        service.human_path(key()) if human else service.get(key(), 'jwt')
    assert error.value.code == 'OBJECT_CORRUPT'


def test_retry_rejects_oversized_existing_object(service):
    service.put(key(), b'abc', 14, 'jwt')
    service.max_bytes = 4
    service.object_path(key()).write_bytes(b'x' * 5)
    with pytest.raises(service.error_type) as error:
        service.put(key(), b'abc', 14, 'jwt')
    assert error.value.code == 'OBJECT_CORRUPT'


def test_metadata_read_is_bounded(service):
    service.put(key(), b'abc', 14, 'jwt')
    path = service.object_path(key())
    meta = path.with_name(path.name + '.metadata.json')
    meta.write_text(' ' * 65537 + meta.read_text())
    with pytest.raises(service.error_type) as error:
        service.get(key(), 'jwt')
    assert error.value.code == 'METADATA_CORRUPT'
