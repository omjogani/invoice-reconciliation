#!/usr/bin/env bash
# One compile job per carrier contract (the compile fan-out's source list).
source "$(dirname "$0")/common.sh"
recon plan-compile --carriers "${FLOWSTATE_VAR_carriers_config:?}" --shipments "${FLOWSTATE_VAR_shipments_path:?}" \
  --out-dir "$(dirname "${FLOWSTATE_VAR_compile_plan_path:?}")" --flowstate-var compile_jobs
