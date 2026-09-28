#!/usr/bin/env bash
# Record the final pair of cards (revised if a revision round ran) for the parent's rates_check.
set -euo pipefail
PY="$FACTORY_ROOT/orchestrator/.venv/bin/python"
if [ -n "${FLOWSTATE_VAR_compare_report_2:-}" ]; then
  A="$FLOWSTATE_VAR_card_a_rev"; B="$FLOWSTATE_VAR_card_b_rev"; REPORT="$FLOWSTATE_VAR_compare_report_2"
else
  A="$FLOWSTATE_VAR_card_a"; B="$FLOWSTATE_VAR_card_b"; REPORT="$FLOWSTATE_VAR_compare_report"
fi
cd "$FACTORY_ROOT"
exec "$PY" -m recon finalize-compile --carrier "${FLOWSTATE_VAR_carrier:?}" --a "$A" --b "$B" \
  --report "$REPORT" --out "${FLOWSTATE_VAR_compile_result:?}" --run-dir "${FLOWSTATE_RUN_DIR:?}"
