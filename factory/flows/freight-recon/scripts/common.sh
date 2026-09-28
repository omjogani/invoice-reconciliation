# Sourced by every script in this flow: run from the repository root on the venv.
set -euo pipefail
PY="$FACTORY_ROOT/orchestrator/.venv/bin/python"
cd "$FACTORY_ROOT"
recon() { "$PY" -m recon "$@"; }
