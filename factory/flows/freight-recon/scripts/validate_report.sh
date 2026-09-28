#!/usr/bin/env bash
# Gate: the assembled report and memos pass every invariant, or the run stops here.
source "$(dirname "$0")/common.sh"
recon validate-report --out-dir "$(dirname "${FLOWSTATE_VAR_report_path:?}")" \
  --work "$(dirname "${FLOWSTATE_VAR_documents_path:?}")" --reconciled "${FLOWSTATE_VAR_reconciled_path:?}" \
  --batches-dir "$(dirname "${FLOWSTATE_VAR_review_plan_path:?}")" > "${FLOWSTATE_VAR_validation_path:?}"
