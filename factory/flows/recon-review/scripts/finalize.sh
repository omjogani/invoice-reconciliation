#!/usr/bin/env bash
# Latest passing attempt becomes the batch's review; otherwise a failure record.
set -euo pipefail
PY="$FACTORY_ROOT/orchestrator/.venv/bin/python"
ATTEMPTS=("${FLOWSTATE_VAR_review_1:?}")
[ -n "${FLOWSTATE_VAR_review_2:-}" ] && ATTEMPTS+=("$FLOWSTATE_VAR_review_2")
[ -n "${FLOWSTATE_VAR_review_3:-}" ] && ATTEMPTS+=("$FLOWSTATE_VAR_review_3")
cd "$FACTORY_ROOT"
exec "$PY" -m recon finalize-review --batch "${FLOWSTATE_VAR_batch_path:?}" \
  --out "${FLOWSTATE_VAR_review_output:?}" "${ATTEMPTS[@]}"
