#!/usr/bin/env bash
# Gate (D1): card checks, agreement and approved snapshot for every contract.
# Emits FLOWSTATE_OUTPUT_rates_verdict=ok|needs_approval|hold. Used by rates_check and rates_recheck.
source "$(dirname "$0")/common.sh"
if [ "${FLOWSTATE_PHASE:?}" = "rates_recheck" ]; then OUT="$(dirname "${FLOWSTATE_VAR_rates_report_2:?}")"
else OUT="$(dirname "${FLOWSTATE_VAR_rates_report:?}")"; fi
mkdir -p "$OUT"
printf '%s' "${FLOWSTATE_VAR_compile_results:?}" > "$OUT/compile-results.json"
recon rates-check --results "$OUT/compile-results.json" --carriers "${FLOWSTATE_VAR_carriers_config:?}" \
  --store "${FLOWSTATE_VAR_approved_store:?}" --out-dir "$OUT" --flowstate-var rates_verdict
