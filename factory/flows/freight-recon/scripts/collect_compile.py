#!/usr/bin/env python3
"""Join reducer: record each contract's compile result path, keyed by branch."""
import json, os
summary = json.loads(os.environ.get("FLOWSTATE_VAR_compile_results") or "{}")
summary[os.environ["FLOWSTATE_BRANCH_ID"]] = os.environ.get("FLOWSTATE_VAR_compile_result", "")
print("FLOWSTATE_OUTPUT_compile_results=" + json.dumps(summary, sort_keys=True))
