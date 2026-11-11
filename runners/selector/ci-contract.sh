#!/usr/bin/env bash
set -euo pipefail

shellcheck scripts/probe.sh
python3 route.py --help >/dev/null
grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' VERSION
python3 - <<'PY'
from unittest.mock import Mock

from route import HOSTED, SELF_HOSTED, VARIABLE, reconcile

for status, current, expected in (
    ("online", HOSTED, SELF_HOSTED),
    ("offline", SELF_HOSTED, HOSTED),
):
    runner = {"name": SELF_HOSTED, "status": status, "labels": [{"name": SELF_HOSTED}]}
    api = Mock(side_effect=[{"runners": [runner]}, {"value": current}, None])
    assert reconcile(api) == expected
    api.assert_called_with("PATCH", VARIABLE, {"value": expected})
PY
