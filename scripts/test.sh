#!/usr/bin/env bash
# Run the recon unit tests on the orchestrator venv (stdlib unittest only).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/orchestrator/.venv/bin/python"
[ -x "$PY" ] || { echo "run orchestrator/setup.sh first" >&2; exit 1; }
cd "$ROOT"
exec "$PY" -m unittest discover -s tests -t . "$@"
