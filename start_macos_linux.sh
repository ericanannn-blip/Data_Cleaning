#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if command -v python3.12 >/dev/null 2>&1; then
    exec python3.12 start_local.py "$@"
fi
exec python3 start_local.py "$@"
