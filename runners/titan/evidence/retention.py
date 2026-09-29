#!/usr/bin/env python3
"""Narrowly scoped expiration. Defaults to a dry run; never run from CI jobs."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from server import KEY_RE, MARKER, _fsync_dir

DAYS = {'required-ci': 14, 'full-certification': 30, 'live-provider': 30, 'runtime-sbom': 90}


def expire(root: Path, *, now=None, apply=False, require_mount=True):
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    if (root.is_symlink() or not (root / MARKER).is_file() or (root / MARKER).is_symlink()
            or (require_mount and not os.path.ismount(root))):
        raise RuntimeError('storage unavailable')
    locks = root / '.locks'
    locks.mkdir(mode=0o700, exist_ok=True)
    if locks.is_symlink():
        raise RuntimeError('unsafe lock directory')
    report = {'would_delete': [], 'deleted': []}
    for meta in sorted(root.rglob('*.tar.gz.metadata.json')):
        if meta.is_symlink() or any(p.is_symlink() for p in meta.parents):
            continue
        obj = meta.with_name(meta.name.removesuffix('.metadata.json'))
        key = obj.relative_to(root).as_posix()
        match = KEY_RE.fullmatch(key)
        if not match or any(p in {'.', '..'} for p in key.split('/')):
            continue
        fd = os.open(locks / (hashlib.sha256(key.encode()).hexdigest() + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with meta.open('rb') as source:
                    payload = source.read(65537)
                if len(payload) > 65536:
                    continue
                metadata = json.loads(payload)
                if not isinstance(metadata, dict):
                    continue
                created = datetime.fromisoformat(metadata['created_at'])
                expires = datetime.fromisoformat(metadata['expires_at'])
                if (metadata.get('version') != 1 or metadata.get('key') != key or metadata.get('legal_hold')
                        or expires > now or expires < created + timedelta(days=DAYS[match['profile']])
                        or obj.is_symlink() or not obj.is_file()):
                    continue
                report['would_delete'].append(key)
                if apply:
                    # Remove visibility first. An interrupted purge never exposes missing content.
                    meta.unlink()
                    _fsync_dir(meta.parent)
                    obj.unlink()
                    _fsync_dir(obj.parent)
                    report['deleted'].append(key)
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/srv/evidence'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(expire(args.root, apply=args.apply), sort_keys=True))


if __name__ == '__main__':
    main()
