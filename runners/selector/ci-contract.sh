#!/usr/bin/env bash
set -euo pipefail

shellcheck scripts/probe.sh
python3 route.py --help >/dev/null
grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' VERSION
