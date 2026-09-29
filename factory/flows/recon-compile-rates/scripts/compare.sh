#!/usr/bin/env bash
# Gate: both cards pass every rate-card check and agree. Emits FLOWSTATE_OUTPUT_agree=yes|no.
# First round (node compare) checks card_a/card_b; the revision round (compare_2) the revised pair.
set -euo pipefail
PY="$FACTORY_ROOT/orchestrator/.venv/bin/python"
if [ "${FLOWSTATE_PHASE:-}" = "compare_2" ]; then
  A="${FLOWSTATE_VAR_card_a_rev:?}"; B="${FLOWSTATE_VAR_card_b_rev:?}"; OUT="${FLOWSTATE_VAR_compare_report_2:?}"
else
  A="${FLOWSTATE_VAR_card_a:?}"; B="${FLOWSTATE_VAR_card_b:?}"; OUT="${FLOWSTATE_VAR_compare_report:?}"
fi
cd "$FACTORY_ROOT"
exec "$PY" -m recon compare-cards --a "$A" --b "$B" --contract "${FLOWSTATE_VAR_contract_path:?}" \
  --carrier "${FLOWSTATE_VAR_carrier:?}" --vocabulary "${FLOWSTATE_VAR_vocabulary_path:?}" \
  --out "$OUT" --flowstate-var agree
