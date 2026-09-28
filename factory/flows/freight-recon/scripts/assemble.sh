#!/usr/bin/env bash
# Build reconciliation-report.json and memos/ in the run's out/ directory.
source "$(dirname "$0")/common.sh"
recon assemble --reconciled "${FLOWSTATE_VAR_reconciled_path:?}" --work "$(dirname "${FLOWSTATE_VAR_documents_path:?}")" \
  --batches-dir "$(dirname "${FLOWSTATE_VAR_review_plan_path:?}")" --reviews "${FLOWSTATE_VAR_reviews_map:?}" \
  --out-dir "$(dirname "${FLOWSTATE_VAR_report_path:?}")" >&2
