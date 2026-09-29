from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETENTION = ROOT / "evidence" / "retention.py"


def load_retention():
    sys.path.insert(0, str(RETENTION.parent))
    spec = importlib.util.spec_from_file_location("evidence_retention", RETENTION)
    module = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module); return module


def write_object(root: Path, name: str, expires: datetime, *, hold: bool = False) -> Path:
    path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"data")
    path.with_name(path.name + ".metadata.json").write_text(json.dumps({
        "version": 1, "key": name, "sha256": "0" * 64, "size": 4,
        "created_at": (expires - timedelta(days=14)).isoformat(), "expires_at": expires.isoformat(), "legal_hold": hold}))
    return path


def test_expiry_is_dry_run_first_scoped_and_honors_legal_hold(tmp_path: Path) -> None:
    module = load_retention(); root = tmp_path / "store"; root.mkdir(); (root / ".titan-evidence-store-v1").write_text("ok")
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    expired = write_object(root, "PintjesB/titan-stocks/" + "a"*40 + "/1/1/job/required-ci.tar.gz", now - timedelta(seconds=1))
    held = write_object(root, "PintjesB/titan-stocks/" + "b"*40 + "/2/1/job/required-ci.tar.gz", now - timedelta(days=1), hold=True)
    unrelated = root / "operator-note"; unrelated.write_text("keep")
    report = module.expire(root, now=now, apply=False, require_mount=False)
    assert report["would_delete"] == [str(expired.relative_to(root))]
    assert expired.exists() and held.exists() and unrelated.exists()
    applied = module.expire(root, now=now, apply=True, require_mount=False)
    assert applied["deleted"] == [str(expired.relative_to(root))]
    assert not expired.exists() and held.exists() and unrelated.exists()

