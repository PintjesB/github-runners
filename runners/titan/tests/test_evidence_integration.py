"""Real TLS client, signed JWT verification, immutable disk storage and restore."""
import hashlib
import json
import os
import shutil
import ssl
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
import urllib.request

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_evidence_client import Handler, https_server, run_client
from test_evidence_service import key, load_server, service


def test_real_tls_oidc_round_trip_restart_restore_and_human_access(https_server, service, tmp_path):
    oidc, cert = https_server
    module = load_server()
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk['kid'] = 'integration'
    jwks = tmp_path / 'keys.json'
    jwks.write_text(json.dumps({'keys': [jwk]}))
    claims = {**service.token_verifier(''), 'iss': 'https://token.actions.githubusercontent.com',
              'aud': 'titan-evidence-v1', 'sub': 'fixture', 'iat': int(time.time()),
              'nbf': int(time.time()), 'exp': int(time.time()) + 120}
    Handler.oidc_value = jwt.encode(claims, signing_key, algorithm='RS256', headers={'kid': 'integration'})
    service.token_verifier = module.JwtVerifier(issuer=claims['iss'], audience=claims['aud'], jwks_file=jwks)
    proxy = tmp_path / 'proxy-secret'
    proxy.write_text('fixture-proxy-secret')
    server = ThreadingHTTPServer(('127.0.0.1', 0), module.make_handler(service, proxy))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, cert.with_name('key.pem'))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    base = f'https://localhost:{server.server_address[1]}/downloads'
    service.download_base = base
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = tmp_path / 'evidence.tar.gz'
        source.write_bytes(os.urandom(2 * 1024 * 1024))
        env = {'TITAN_EVIDENCE_BASE_URL': base}
        put = run_client(oidc, cert, 'put', '--file', str(source), '--key', key(), '--retention-days', '14', extra_env=env)
        assert put.returncode == 0, put.stderr
        receipt = json.loads(put.stdout)
        assert receipt['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
        out = tmp_path / 'readback'
        get = run_client(oidc, cert, 'get', '--key', key(), '--output', str(out), extra_env=env)
        assert get.returncode == 0, get.stderr
        assert out.read_bytes() == source.read_bytes()
        restarted = module.EvidenceService(service.root, service.policy, service.token_verifier,
                                           service.metadata_resolver, download_base=base, require_mount=False)
        assert restarted.get(key(), Handler.oidc_value) == source.read_bytes()
        restored_root = tmp_path / 'restored'
        shutil.copytree(service.root, restored_root)
        restored = module.EvidenceService(restored_root, service.policy, service.token_verifier,
                                          service.metadata_resolver, download_base=base, require_mount=False)
        assert restored.get(key(), Handler.oidc_value) == source.read_bytes()
        tls = ssl.create_default_context(cafile=str(cert))
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(base + '/' + key(), context=tls)
        assert denied.value.code == 401
        request = urllib.request.Request(base + '/' + key(), headers={
            'X-Evidence-Proxy-Secret': 'fixture-proxy-secret', 'X-Forwarded-User': 'authorized-fixture'})
        with urllib.request.urlopen(request, context=tls) as response:
            assert response.headers['X-Content-Type-Options'] == 'nosniff'
            assert response.headers['Content-Disposition'].startswith('attachment;')
            assert response.read() == source.read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        Handler.oidc_value = 'signed-token'
