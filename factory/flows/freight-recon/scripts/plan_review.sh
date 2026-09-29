#!/usr/bin/env bash
# Batch the non-accept items; emits review_jobs (fan-out source) and review_state=none|some.
source "$(dirname "$0")/common.sh"
recon plan-review --reconciled "${FLOWSTATE_VAR_reconciled_path:?}" --carriers "${FLOWSTATE_VAR_carriers_config:?}" \
  --as-of "${FLOWSTATE_VAR_as_of_date:?}" --out-dir "$(dirname "${FLOWSTATE_VAR_review_plan_path:?}")" \
  --flowstate-var review_jobs --flowstate-count-var review_state
