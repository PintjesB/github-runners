#!/usr/bin/env python3
"""Immutable, run-scoped GitHub Actions evidence service."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import unquote, urlsplit


MARKER = ".titan-evidence-store-v1"
MAX_BYTES = 512 * 1024 * 1024
KEY_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?P<sha>[0-9a-f]{40})/(?P<run>[1-9][0-9]*)/(?P<attempt>[1-9][0-9]*)/"
    r"(?P<job>[A-Za-z0-9_-]+)/(?P<profile>required-ci|full-certification|live-provider|runtime-sbom)\.tar\.gz"
)
REQUIRED_CLAIMS = (
    "repository", "repository_id", "repository_owner_id", "sha", "run_id",
    "run_attempt", "workflow_ref", "actor", "event_name", "check_run_id",
)


class EvidenceError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"empty secret file: {path}")
    return value


class JwtVerifier:
    """Verify GitHub OIDC JWTs with PyJWT and a pinned issuer/audience."""

    def __init__(self, *, issuer: str, audience: str, jwks_url: str | None = None,
                 jwks_file: Path | None = None):
        import jwt
        self.jwt = jwt
        self.issuer = issuer
        self.audience = audience
        self.jwks_file = jwks_file
        self.jwks_client = jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300) if jwks_url else None
        if not self.jwks_client and not self.jwks_file:
            raise ValueError("jwks_url or jwks_file is required")

    def __call__(self, token: str) -> dict:
        try:
            if self.jwks_file:
                data = json.loads(self.jwks_file.read_text(encoding="utf-8"))
                header = self.jwt.get_unverified_header(token)
                matching = [item for item in data.get("keys", []) if item.get("kid") == header.get("kid")]
                if len(matching) != 1:
                    raise ValueError("unknown signing key")
                key = self.jwt.PyJWK.from_dict(matching[0]).key
            else:
                key = self.jwks_client.get_signing_key_from_jwt(token).key
            return self.jwt.decode(
                token, key=key, algorithms=["RS256"], audience=self.audience,
                issuer=self.issuer, leeway=5,
                options={"require": ["exp", "nbf", "iat", "sub", *REQUIRED_CLAIMS]},
            )
        except Exception as exc:
            raise EvidenceError(401, "INVALID_IDENTITY") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise EvidenceError(503, "REDIRECT_REFUSED")


class GitHubMetadataResolver:
    """Resolve trusted workflow-run metadata using a service-only credential."""

    def __init__(self, token_file: Path, api_base: str = "https://api.github.com"):
        self.token_file = token_file
        self.api_base = api_base.rstrip("/")
        if urlsplit(self.api_base).scheme != "https":
            raise ValueError("GitHub API base must use HTTPS")

    def _get(self, path: str) -> dict:
        token = _read_secret(self.token_file)
        request = urllib.request.Request(
            self.api_base + path,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "titan-evidence/1"},
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise EvidenceError(503, "METADATA_UNAVAILABLE") from exc

    def __call__(self, repository: str, run_id: str, attempt: str) -> dict:
        base = f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}"
        run = self._get(base)
        jobs = self._get(base + "/jobs?per_page=100")
        head_repository = (run.get("head_repository") or {}).get("full_name")
        return {"head_repository": head_repository, "jobs": [
            {"name": item.get("name"), "labels": item.get("labels") or [],
             "check_run_id": str(item.get("check_run_url", "")).rsplit("/", 1)[-1]}
            for item in jobs.get("jobs", [])
        ]}


class EvidenceService:
    def __init__(self, root: Path, policy: dict, token_verifier: Callable[[str], dict],
                 metadata_resolver: Callable[[str, str, str], dict], *, download_base: str,
                 require_mount: bool = True, max_bytes: int = MAX_BYTES):
        self.root = Path(root)
        self.policy = policy
        self.token_verifier = token_verifier
        self.metadata_resolver = metadata_resolver
        self.download_base = download_base.rstrip("/")
        self.require_mount = require_mount
        self.max_bytes = max_bytes
        self.error_type = EvidenceError
        self.ensure_ready()

    def ensure_ready(self) -> None:
        try:
            if not self.root.is_dir() or self.root.is_symlink():
                raise EvidenceError(503, "STORAGE_UNAVAILABLE")
            if not (self.root / MARKER).is_file() or (self.root / MARKER).is_symlink():
                raise EvidenceError(503, "STORAGE_UNAVAILABLE")
            if self.require_mount and not os.path.ismount(self.root):
                raise EvidenceError(503, "STORAGE_UNAVAILABLE")
            for name in (".staging", ".locks"):
                path = self.root / name
                path.mkdir(mode=0o700, exist_ok=True)
                if path.is_symlink() or not path.is_dir():
                    raise EvidenceError(503, "STORAGE_UNAVAILABLE")
        except OSError as exc:
            raise EvidenceError(503, "STORAGE_UNAVAILABLE") from exc

    def _parse(self, key: str) -> dict[str, str]:
        match = KEY_RE.fullmatch(key)
        if not match or any(part in {".", ".."} for part in key.split("/")):
            raise EvidenceError(400, "INVALID_KEY")
        return match.groupdict()

    def _safe_parent(self, parts: list[str]) -> Path:
        current = self.root
        for part in parts:
            current = current / part
            try:
                current.mkdir(mode=0o750)
                _fsync_dir(current.parent)
            except FileExistsError:
                pass
            try:
                mode = current.lstat().st_mode
            except OSError as exc:
                raise EvidenceError(503, "STORAGE_UNAVAILABLE") from exc
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise EvidenceError(409, "UNSAFE_STORAGE_PATH")
        return current

    def object_path(self, key: str) -> Path:
        self._parse(key)
        return self.root.joinpath(*key.split("/"))

    def _authorize(self, key: str, token: str) -> tuple[dict[str, str], dict]:
        parsed = self._parse(key)
        claims = self.token_verifier(token)
        if any(not isinstance(claims.get(name), (str, int)) for name in REQUIRED_CLAIMS):
            raise EvidenceError(401, "INVALID_IDENTITY")
        repository = f"{parsed['owner']}/{parsed['repo']}"
        repo_policy = self.policy.get("repositories", {}).get(repository)
        if not repo_policy:
            raise EvidenceError(403, "REPOSITORY_DENIED")
        expected = {"repository": repository, "repository_id": str(repo_policy["repository_id"]),
                    "repository_owner_id": str(repo_policy["owner_id"]), "sha": parsed["sha"],
                    "run_id": parsed["run"], "run_attempt": parsed["attempt"]}
        if any(str(claims.get(name)) != value for name, value in expected.items()):
            raise EvidenceError(403, "RUN_SCOPE_DENIED")
        if claims.get("event_name") not in {"push", "pull_request", "workflow_dispatch", "schedule"} or claims.get("actor") == "dependabot[bot]":
            raise EvidenceError(403, "UNTRUSTED_EVENT")
        prefix = repository + "/"
        workflow_ref = str(claims["workflow_ref"])
        if not workflow_ref.startswith(prefix) or "@" not in workflow_ref:
            raise EvidenceError(403, "WORKFLOW_DENIED")
        workflow_path = workflow_ref[len(prefix):].rsplit("@", 1)[0]
        profile_policy = repo_policy.get("workflows", {}).get(workflow_path, {}).get(parsed["profile"])
        expected_job = (profile_policy or {}).get("jobs", {}).get(parsed["job"])
        if not expected_job:
            raise EvidenceError(403, "WORKFLOW_DENIED")
        try:
            metadata = self.metadata_resolver(repository, parsed["run"], parsed["attempt"])
        except EvidenceError:
            raise
        except Exception as exc:
            raise EvidenceError(503, "METADATA_UNAVAILABLE") from exc
        if metadata.get("head_repository") != repository:
            raise EvidenceError(403, "UNTRUSTED_SOURCE")
        label = repo_policy.get("trusted_runner_label")
        matching = [job for job in metadata.get("jobs", []) if job.get("name") == expected_job
                    and str(job.get("check_run_id", "")) == str(claims["check_run_id"])]
        if len(matching) != 1 or label not in matching[0].get("labels", []):
            raise EvidenceError(403, "RUNNER_ADMISSION_DENIED")
        return parsed, profile_policy

    def _lock(self, key: str):
        path = self.root / ".locks" / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
        handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _read_metadata(self, path: Path) -> dict | None:
        meta = path.with_name(path.name + ".metadata.json")
        if not meta.exists():
            return None
        if meta.is_symlink() or not meta.is_file():
            raise EvidenceError(409, "UNSAFE_STORAGE_PATH")
        try:
            with meta.open("rb") as source:
                payload = source.read(65537)
            if len(payload) > 65536:
                raise ValueError("metadata too large")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise ValueError("metadata must be an object")
            return result
        except (OSError, ValueError) as exc:
            raise EvidenceError(503, "METADATA_CORRUPT") from exc

    def _write_metadata(self, path: Path, metadata: dict) -> None:
        target = path.with_name(path.name + ".metadata.json")
        fd, temp_name = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
        try:
            payload = (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode()
            os.write(fd, payload); os.fsync(fd); os.close(fd); fd = -1
            os.replace(temp_name, target)
            _fsync_dir(path.parent)
        finally:
            if fd >= 0: os.close(fd)
            try: os.unlink(temp_name)
            except FileNotFoundError: pass

    def _receipt(self, key: str, digest: str, size: int) -> dict:
        return {"version": 1, "key": key, "sha256": digest, "size": size,
                "url": f"{self.download_base}/{key}"}

    def put(self, key: str, source: bytes | BinaryIO, retention_days: int, token: str) -> dict:
        self.ensure_ready()
        parsed, profile_policy = self._authorize(key, token)
        if retention_days != int(profile_policy["retention_days"]):
            raise EvidenceError(400, "INVALID_RETENTION")
        staging_dir = self.root / ".staging"
        fd, stage_name = tempfile.mkstemp(prefix="upload-", dir=staging_dir)
        size = 0; digest = hashlib.sha256()
        try:
            stream = source if hasattr(source, "read") else None
            output = os.fdopen(fd, "wb", closefd=True)
            fd = -1
            with output:
                while True:
                    chunk = stream.read(1024 * 1024) if stream else source[size:size + 1024 * 1024]
                    if not chunk: break
                    size += len(chunk)
                    if size > self.max_bytes: raise EvidenceError(413, "UPLOAD_TOO_LARGE")
                    output.write(chunk); digest.update(chunk)
                output.flush(); os.fsync(output.fileno())
            fd = -1
            parent = self._safe_parent(key.split("/")[:-1]); target = parent / key.split("/")[-1]
            lock = self._lock(key)
            try:
                hexdigest = digest.hexdigest()
                if target.exists():
                    if target.is_symlink() or not target.is_file(): raise EvidenceError(409, "UNSAFE_STORAGE_PATH")
                    existing_digest = self._digest_object(target)
                    if existing_digest != hexdigest or target.stat().st_size != size:
                        raise EvidenceError(409, "OBJECT_CONFLICT")
                else:
                    try: os.link(stage_name, target, follow_symlinks=False)
                    except FileExistsError: raise EvidenceError(409, "OBJECT_CONFLICT")
                    _fsync_dir(parent)
                now = _utcnow(); old = self._read_metadata(target)
                expiry = now + timedelta(days=retention_days)
                if old:
                    try: expiry = max(expiry, datetime.fromisoformat(old["expires_at"]))
                    except (KeyError, ValueError, TypeError) as exc: raise EvidenceError(503, "METADATA_CORRUPT") from exc
                metadata = {"version": 1, "key": key, "sha256": hexdigest, "size": size,
                            "created_at": old.get("created_at") if old else now.isoformat(),
                            "expires_at": expiry.isoformat(), "legal_hold": bool((old or {}).get("legal_hold", False))}
                self._write_metadata(target, metadata)
                return self._receipt(key, hexdigest, size)
            finally:
                lock.close()
        except EvidenceError:
            raise
        except OSError as exc:
            status = 507 if exc.errno in (errno.ENOSPC, errno.EDQUOT) else 503
            raise EvidenceError(status, "STORAGE_WRITE_FAILED") from exc
        finally:
            if fd >= 0: os.close(fd)
            try: os.unlink(stage_name)
            except FileNotFoundError: pass

    def checked_path(self, key: str) -> Path:
        self._parse(key)
        current = self.root
        for part in key.split("/")[:-1]:
            current = current / part
            if current.is_symlink() or not current.is_dir():
                raise EvidenceError(404, "OBJECT_NOT_FOUND")
        return current / key.rsplit("/", 1)[-1]

    def get_path(self, key: str, token: str) -> Path:
        self.ensure_ready(); self._authorize(key, token)
        path = self.checked_path(key); metadata = self._read_metadata(path)
        if not metadata or not path.is_file() or path.is_symlink(): raise EvidenceError(404, "OBJECT_NOT_FOUND")
        self._verify_object(path, metadata)
        return path

    def get(self, key: str, token: str) -> bytes:
        with self._lock(key):
            return self.get_path(key, token).read_bytes()

    def _digest_object(self, path: Path) -> str:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            if os.fstat(source.fileno()).st_size > self.max_bytes:
                raise EvidenceError(503, "OBJECT_CORRUPT")
            while chunk := source.read(min(1024 * 1024, self.max_bytes - size + 1)):
                size += len(chunk)
                if size > self.max_bytes:
                    raise EvidenceError(503, "OBJECT_CORRUPT")
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_object(self, path: Path, metadata: dict) -> None:
        if (path.stat().st_size != metadata.get("size")
                or self._digest_object(path) != metadata.get("sha256")):
            raise EvidenceError(503, "OBJECT_CORRUPT")

    def human_path(self, key: str) -> Path:
        self.ensure_ready(); path = self.checked_path(key); metadata = self._read_metadata(path)
        if not metadata or not path.is_file() or path.is_symlink(): raise EvidenceError(404, "OBJECT_NOT_FOUND")
        self._verify_object(path, metadata)
        return path


def make_handler(service: EvidenceService, proxy_secret_file: Path | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "titan-evidence/1"
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, format: str, *args: object) -> None:
            sys.stderr.write("evidence request completed\n")

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close"); self.close_connection = True
            self.end_headers(); self.wfile.write(body)

        def _error(self, exc: EvidenceError) -> None:
            self._json(exc.status, {"error": exc.code})

        def _token(self) -> str:
            value = self.headers.get("Authorization", "")
            if not value.startswith("Bearer ") or not value[7:]: raise EvidenceError(401, "AUTHENTICATION_REQUIRED")
            return value[7:]

        def do_GET(self) -> None:  # noqa: N802
            started = time.monotonic()
            try:
                path = urlsplit(self.path).path
                if path == "/healthz":
                    service.ensure_ready(); self._json(200, {"status": "ready"}); return
                if path.startswith("/v1/objects/"):
                    key = unquote(path[len("/v1/objects/"):])
                    service._authorize(key, self._token())
                elif path.startswith("/downloads/"):
                    if not proxy_secret_file: raise EvidenceError(403, "HUMAN_ACCESS_DISABLED")
                    supplied = self.headers.get("X-Evidence-Proxy-Secret", "")
                    if not supplied or not hmac.compare_digest(supplied, _read_secret(proxy_secret_file)):
                        raise EvidenceError(401, "AUTHENTICATION_REQUIRED")
                    if not self.headers.get("X-Forwarded-User"): raise EvidenceError(401, "AUTHENTICATION_REQUIRED")
                    key = unquote(path[len("/downloads/"):])
                else: raise EvidenceError(404, "NOT_FOUND")
                service.ensure_ready()
                service._parse(key)
                with service._lock(key):
                    target = service.human_path(key)
                    with target.open("rb") as source:
                        size = os.fstat(source.fileno()).st_size
                        if size > service.max_bytes: raise EvidenceError(503, "OBJECT_CORRUPT")
                        self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
                        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Content-Length", str(size))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close"); self.close_connection = True
                        self.end_headers()
                        while chunk := source.read(1024 * 1024):
                            remaining = 180 - (time.monotonic() - started)
                            if remaining <= 0: raise TimeoutError()
                            self.connection.settimeout(min(30, remaining))
                            self.wfile.write(chunk)
            except EvidenceError as exc: self._error(exc)
            except Exception: self.close_connection = True

        def do_PUT(self) -> None:  # noqa: N802
            try:
                started = time.monotonic()
                path = urlsplit(self.path).path
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise EvidenceError(400, "INVALID_FRAMING")
                if not path.startswith("/v1/objects/"): raise EvidenceError(404, "NOT_FOUND")
                try: length = int(self.headers.get("Content-Length", ""))
                except ValueError: raise EvidenceError(411, "CONTENT_LENGTH_REQUIRED")
                if length < 0 or length > service.max_bytes: raise EvidenceError(413, "UPLOAD_TOO_LARGE")
                try: retention = int(self.headers.get("X-Evidence-Retention-Days", ""))
                except ValueError: raise EvidenceError(400, "INVALID_RETENTION")
                class Limited:
                    remaining = length
                    def read(inner, size: int) -> bytes:
                        if inner.remaining <= 0: return b""
                        remaining = 180 - (time.monotonic() - started)
                        if remaining <= 0: raise EvidenceError(408, "UPLOAD_DEADLINE")
                        self.connection.settimeout(min(30, remaining))
                        data = self.rfile.read1(min(size, inner.remaining)); inner.remaining -= len(data)
                        if not data and inner.remaining: raise EvidenceError(400, "INTERRUPTED_UPLOAD")
                        return data
                receipt = service.put(unquote(path[len("/v1/objects/"):]), Limited(), retention, self._token())
                self._json(200, receipt)
            except EvidenceError as exc: self._error(exc)
            except Exception: self._error(EvidenceError(503, "SERVICE_UNAVAILABLE"))
    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--check", action="store_true"); args = parser.parse_args()
    root = Path(os.environ.get("EVIDENCE_STORAGE_ROOT", "/srv/evidence"))
    policy_path = Path(os.environ.get("EVIDENCE_POLICY_FILE", "/run/config/evidence-policy.json"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    verifier = JwtVerifier(issuer=os.environ.get("EVIDENCE_OIDC_ISSUER", "https://token.actions.githubusercontent.com"),
                           audience=os.environ["EVIDENCE_OIDC_AUDIENCE"],
                           jwks_url=os.environ.get("EVIDENCE_OIDC_JWKS_URL", "https://token.actions.githubusercontent.com/.well-known/jwks"))
    resolver = GitHubMetadataResolver(Path(os.environ.get("EVIDENCE_GITHUB_TOKEN_FILE", "/run/secrets/github_api_token")))
    service = EvidenceService(root, policy, verifier, resolver, download_base=os.environ["EVIDENCE_DOWNLOAD_BASE_URL"])
    if args.check: service.ensure_ready(); return 0
    address = os.environ.get("EVIDENCE_LISTEN", "127.0.0.1:8080"); host, port = address.rsplit(":", 1)
    proxy_file_value = os.environ.get("EVIDENCE_PROXY_SECRET_FILE", "/run/secrets/proxy_secret")
    server = ThreadingHTTPServer((host, int(port)), make_handler(service, Path(proxy_file_value) if proxy_file_value else None))
    server.daemon_threads = True
    server.timeout = 180
    server.serve_forever(); return 0


if __name__ == "__main__":
    raise SystemExit(main())

