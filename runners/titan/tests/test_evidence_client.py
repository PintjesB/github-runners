from __future__ import annotations

import json
import hashlib
import os
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "scripts" / "titan-evidence"


class Handler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, bytes, dict[str, str]]] = []
    oidc_value = "signed-token"
    response_status = 200
    response_body = b""
    redirect = False

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/oidc"):
            self._send(200, json.dumps({"value": self.oidc_value}).encode())
            return
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", "https://attacker.invalid/stolen")
            self.end_headers()
            return
        self.requests.append(("GET", self.path, b"", dict(self.headers)))
        self._send(self.response_status, self.response_body)

    def do_PUT(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", "https://attacker.invalid/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.requests.append(("PUT", self.path, body, dict(self.headers)))
        self._send(self.response_status, self.response_body)

    def log_message(self, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def https_server(tmp_path: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    Handler.requests, Handler.response_status, Handler.response_body, Handler.redirect = [], 200, b"", False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, cert_path
    finally:
        server.shutdown(); thread.join()


def run_client(server: ThreadingHTTPServer, cert: Path, *args: str, extra_env: dict[str, str] | None = None):
    port = server.server_address[1]
    env = {"PATH": os.environ["PATH"], "TITAN_EVIDENCE_BASE_URL": f"https://localhost:{port}/downloads",
           "TITAN_EVIDENCE_AUDIENCE": "titan-evidence-v1",
           "ACTIONS_ID_TOKEN_REQUEST_URL": f"https://localhost:{port}/oidc?x=1",
           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-secret", "SSL_CERT_FILE": str(cert)}
    env.update(extra_env or {})
    return subprocess.run([str(CLIENT), *args], text=True, capture_output=True, env=env, timeout=10)


def test_put_and_get_use_fresh_oidc_and_exact_receipt(https_server, tmp_path: Path) -> None:
    server, cert = https_server
    source = tmp_path / "bundle.tar.gz"; source.write_bytes(b"gzip bytes")
    key = "PintjesB/titan-stocks/" + "a" * 40 + "/12/3/required-ci/required-ci.tar.gz"
    receipt = {"version": 1, "key": key, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "size": len(source.read_bytes()),
               "url": f"https://localhost:{server.server_address[1]}/downloads/{key}"}
    Handler.response_body = json.dumps(receipt).encode()
    put = run_client(server, cert, "put", "--file", str(source), "--key", key, "--retention-days", "14")
    assert put.returncode == 0, put.stderr
    assert json.loads(put.stdout) == receipt
    method, path, body, headers = Handler.requests[-1]
    assert (method, path, body) == ("PUT", f"/v1/objects/{key}", b"gzip bytes")
    assert headers["Authorization"] == "Bearer signed-token"
    assert headers["X-Evidence-Retention-Days"] == "14"
    assert "request-secret" not in put.stderr + put.stdout
    Handler.response_body = b"stored bytes"
    output = tmp_path / "readback.tar.gz"
    get = run_client(server, cert, "get", "--key", key, "--output", str(output))
    assert get.returncode == 0, get.stderr
    assert output.read_bytes() == b"stored bytes"
    assert Handler.requests[-1][0:2] == ("GET", f"/v1/objects/{key}")


def test_client_fails_closed_for_invalid_configuration_redirects_and_existing_output(https_server, tmp_path: Path) -> None:
    server, cert = https_server
    key = "PintjesB/titan-stocks/" + "a" * 40 + "/12/3/job/required-ci.tar.gz"
    source = tmp_path / "bundle.tar.gz"; source.write_bytes(b"x")
    bad = run_client(server, cert, "put", "--file", str(source), "--key", key, "--retention-days", "14",
                     extra_env={"TITAN_EVIDENCE_BASE_URL": "http://localhost/downloads"})
    assert bad.returncode != 0
    Handler.redirect = True
    redirected = run_client(server, cert, "put", "--file", str(source), "--key", key, "--retention-days", "14")
    assert redirected.returncode != 0
    assert "signed-token" not in redirected.stderr
    Handler.redirect = False
    existing = tmp_path / "existing"; existing.write_bytes(b"keep")
    refused = run_client(server, cert, "get", "--key", key, "--output", str(existing))
    assert refused.returncode != 0
    assert existing.read_bytes() == b"keep"


def test_client_missing_oidc_and_invalid_tls_are_secret_safe(https_server, tmp_path: Path) -> None:
    server, cert = https_server
    key = "PintjesB/titan-stocks/" + "a" * 40 + "/12/3/job/required-ci.tar.gz"
    source = tmp_path / "bundle.tar.gz"; source.write_bytes(b"x")
    missing = run_client(server, cert, "put", "--file", str(source), "--key", key, "--retention-days", "14",
                         extra_env={"ACTIONS_ID_TOKEN_REQUEST_URL": ""})
    assert missing.returncode != 0
    invalid_tls = run_client(server, tmp_path / "missing-ca.pem", "put", "--file", str(source), "--key", key, "--retention-days", "14")
    assert invalid_tls.returncode != 0
    assert "request-secret" not in invalid_tls.stderr + invalid_tls.stdout

