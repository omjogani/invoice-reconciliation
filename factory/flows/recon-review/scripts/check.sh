#!/usr/bin/env bash
# Gate: the reviewer contract (D7) on the attempt this check node follows.
# Always exits 0 on a completed check; the verdict is FLOWSTATE_OUTPUT_review_ok=yes|no.
set -euo pipefail
PY="$FACTORY_ROOT/orchestrator/.venv/bin/python"
case "${FLOWSTATE_PHASE:?}" in
  check_1) OUT="$FLOWSTATE_VAR_review_1"; FB="$FLOWSTATE_VAR_feedback_1" ;;
  check_2) OUT="$FLOWSTATE_VAR_review_2"; FB="$FLOWSTATE_VAR_feedback_2" ;;
  check_3) OUT="$FLOWSTATE_VAR_review_3"; FB="$FLOWSTATE_VAR_feedback_3" ;;
  *) echo "check.sh: unexpected phase $FLOWSTATE_PHASE" >&2; exit 1 ;;
esac
cd "$FACTORY_ROOT"
exec "$PY" -m recon check-review --batch "${FLOWSTATE_VAR_batch_path:?}" --output "$OUT" \
  --feedback "$FB" --flowstate-var review_ok
