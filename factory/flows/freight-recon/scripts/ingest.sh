#!/usr/bin/env bash
# Inventory, parse and tie out every billing document; route unknown formats to the fallback agent.
source "$(dirname "$0")/common.sh"
WORK="$(dirname "${FLOWSTATE_VAR_documents_path:?}")"
recon ingest --invoices "${FLOWSTATE_VAR_invoices_dir:?}" --out "$WORK" >&2
UNKNOWN=$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["unknown"]))' "$WORK/inventory.json")
[ "$UNKNOWN" = "0" ] && echo "FLOWSTATE_OUTPUT_unknown_state=none" || echo "FLOWSTATE_OUTPUT_unknown_state=some"
