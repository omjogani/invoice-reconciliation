#!/usr/bin/env bash
# Gate: agent-parsed records are validated and tied out like any parser's output, then merged.
source "$(dirname "$0")/common.sh"
WORK="$(dirname "${FLOWSTATE_VAR_documents_path:?}")"
recon merge-fallback --fallback "${FLOWSTATE_VAR_fallback_path:?}" --work "$WORK" \
  --carriers "${FLOWSTATE_VAR_carriers_config:?}" > "${FLOWSTATE_VAR_fallback_merged_path:?}"
