#!/bin/sh
set -eu

case "$(uname -m)" in
    x86_64) actual=amd64 ;;
    aarch64) actual=arm64 ;;
    *) exit 1 ;;
esac
[ "$actual" = "${EXPECTED_ARCH:?}" ]
[ "$(id -u)" = 1001 ]
python3 /app/route.py --help
