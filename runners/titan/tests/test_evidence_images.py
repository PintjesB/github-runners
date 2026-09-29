from pathlib import Path
import yaml

REPO = Path(__file__).resolve().parents[3]


def test_both_native_images_install_the_same_evidence_client():
    clients = []
    for profile in ('titan', 'oportunist'):
        root = REPO / 'runners' / profile
        dockerfile = (root / 'Dockerfile').read_text()
        assert 'COPY --chown=root:root scripts/titan-evidence /usr/local/bin/titan-evidence' in dockerfile
        assert 'RUN chmod 0755 /usr/local/bin/titan-evidence' in dockerfile
        clients.append((root / 'scripts/titan-evidence').read_bytes())
        assert 'titan-evidence --help' in (root / 'scripts/probe.sh').read_text()
        compose = yaml.safe_load((root / 'docker-compose.yml').read_text())
        env = compose['services']['runner']['environment']
        assert env['TITAN_EVIDENCE_BASE_URL'] == '${TITAN_EVIDENCE_BASE_URL:-}'
        assert env['TITAN_EVIDENCE_AUDIENCE'] == '${TITAN_EVIDENCE_AUDIENCE:-}'
        assert (root / 'VERSION').read_text().strip() == '1.1.0'
    assert clients[0] == clients[1]
