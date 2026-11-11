#!/usr/bin/env bash
set -euo pipefail

shellcheck scripts/probe.sh
python3 route.py --help >/dev/null
grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' VERSION
python3 - <<'PY'
import os
from unittest.mock import Mock, patch

from route import HOSTED, SELF_HOSTED, VARIABLE, main, reconcile

for status, current, expected in (
    ("online", HOSTED, SELF_HOSTED),
    ("offline", SELF_HOSTED, HOSTED),
):
    runner = {"name": SELF_HOSTED, "status": status, "labels": [{"name": SELF_HOSTED}]}
    api = Mock(side_effect=[{"runners": [runner]}, {"value": current}, None])
    assert reconcile(api) == expected
    api.assert_called_with("PATCH", VARIABLE, {"value": expected})

# Regular environment variables work with read-only container filesystems.
# No Compose secret or host token file is required.
with patch.dict(os.environ, {
    "RUNNER_SELECTOR_REPOSITORY": "PintjesB/docker-compose",
    "RUNNER_SELECTOR_TOKEN": "  test-selector-token  ",
}, clear=True), patch("sys.argv", ["route.py", "--dry-run"]):
    with patch("route.request_api", return_value={"runners": []}) as request:
        assert main() == 0
        assert request.call_args.args[1] == "test-selector-token"

with patch.dict(os.environ, {
    "RUNNER_SELECTOR_REPOSITORY": "PintjesB/docker-compose",
    "RUNNER_SELECTOR_TOKEN": " ",
}, clear=True), patch("sys.argv", ["route.py", "--dry-run"]):
    assert main() == 1
PY
