#!/usr/bin/env python3
"""smoke-branch reducer: maintain branch_summary keyed by branch_id.

Runs once per branch arrival at the join. Each branch's scope contains the
path it wrote (note_a for branch a, note_b for branch b). We don't know
which branch fired us without inspecting which note variable is present,
so we accept either and record whichever path the branch produced.

Inputs (env):
  FLOWSTATE_BRANCH_ID         - id of the triggering branch (B01, B02, ...)
  FLOWSTATE_VAR_note_a        - path written by worker_a (set iff branch a)
  FLOWSTATE_VAR_note_b        - path written by worker_b (set iff branch b)
  FLOWSTATE_VAR_branch_summary - prior summary (JSON dict); may be unset on
                                  the first arrival.

Output (stdout):
  FLOWSTATE_OUTPUT_branch_summary=<json dict> - updated summary, with an
    entry for FLOWSTATE_BRANCH_ID containing the note path the branch
    contributed.
"""
import json
import os
import sys

bid = os.environ["FLOWSTATE_BRANCH_ID"]

# Pick up whichever per-branch note variable is set in this branch's scope.
note_path = os.environ.get("FLOWSTATE_VAR_note_a") or os.environ.get("FLOWSTATE_VAR_note_b") or ""

prior_raw = os.environ.get("FLOWSTATE_VAR_branch_summary") or "{}"
try:
    prior = json.loads(prior_raw)
    if not isinstance(prior, dict):
        prior = {}
except (json.JSONDecodeError, ValueError):
    prior = {}

prior[bid] = {"note_path": note_path}

sys.stdout.write("FLOWSTATE_OUTPUT_branch_summary=" + json.dumps(prior) + "\n")
