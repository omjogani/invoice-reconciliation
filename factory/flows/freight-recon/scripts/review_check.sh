#!/usr/bin/env bash
# Gate (D7) across all batches: every batch settled and passing. Fails the run otherwise.
source "$(dirname "$0")/common.sh"
printf '%s' "${FLOWSTATE_VAR_review_outputs:-"{}"}" > "${FLOWSTATE_VAR_reviews_map:?}"
recon review-check --batches-dir "$(dirname "${FLOWSTATE_VAR_review_plan_path:?}")" \
  --reviews "$FLOWSTATE_VAR_reviews_map" --feedback "$(dirname "$FLOWSTATE_VAR_reviews_map")/review-feedback.json" >&2
