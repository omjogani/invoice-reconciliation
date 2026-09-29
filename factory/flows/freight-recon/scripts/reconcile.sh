#!/usr/bin/env bash
# Deterministic reconciliation with the approved rate cards.
source "$(dirname "$0")/common.sh"
if [ -n "${FLOWSTATE_VAR_rates_report_2:-}" ]; then RATES="$(dirname "$FLOWSTATE_VAR_rates_report_2")"
else RATES="$(dirname "${FLOWSTATE_VAR_rates_report:?}")"; fi
recon reconcile --work "$(dirname "${FLOWSTATE_VAR_documents_path:?}")" --shipments "${FLOWSTATE_VAR_shipments_path:?}" \
  --cards "$RATES/rate-cards.json" --out "${FLOWSTATE_VAR_reconciled_path:?}" >&2
