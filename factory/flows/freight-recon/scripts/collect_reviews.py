#!/usr/bin/env python3
"""Join reducer: record each batch's settled review path, keyed by branch."""
import json, os
summary = json.loads(os.environ.get("FLOWSTATE_VAR_review_outputs") or "{}")
summary[os.environ["FLOWSTATE_BRANCH_ID"]] = os.environ.get("FLOWSTATE_VAR_review_output", "")
print("FLOWSTATE_OUTPUT_review_outputs=" + json.dumps(summary, sort_keys=True))
