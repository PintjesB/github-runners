#!/usr/bin/env bash
set -euo pipefail

while IFS= read -r script; do
    bash -n "$script"
done < <(find . -path './tests/__pycache__' -prune -o -type f -name '*.sh' -print)

while IFS= read -r script; do
    shellcheck -S warning "$script"
done < <(find . -path './tests/__pycache__' -prune -o -type f -name '*.sh' -print)

grep -q 'lsb-release' Dockerfile

bash tests/test_runner_scripts.sh
python3 tests/test_dependency_cache.py

# The original Titan contract file predates the monorepo layout. Keep all
# non-publication assertions there and validate repository-level publication
# separately against the real reusable workflow. The old workflow parse/port
# test also references the removed profile-local publish.yml, so its equivalent
# now lives in test_publish_titan_contract.py.
python3 -m pytest tests/test_runner_contract.py -v \
    -k 'not test_publish and not test_workflow_files_parse_and_have_no_executable_fixed_host_ports'
python3 -m pytest tests/test_codex_contract.py -v
python3 -m pytest tests/test_publish_titan_contract.py -v
python3 tests/check_compose_contract.py

python3 -m pytest tests/test_evidence*.py -q
