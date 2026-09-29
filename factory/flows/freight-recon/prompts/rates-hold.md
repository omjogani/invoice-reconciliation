The rate-card gate did not pass, so the run is waiting for a human. You are
the orchestrator: run this step yourself, in this session. **You must never
approve a rate card on the human's behalf**, and never edit a card or the
snapshot store yourself.

## What happened

Read the gate report: `{rates_report}`

- `verdict` is `needs_approval` (a contract version has no approved
  snapshot yet) or `hold` (a card failed a check, the two extractions
  disagreed after their revision round, a clause was unsupported, or the
  agreed reading differs from the approved snapshot).
- `contracts[]` has one entry per carrier with its own `verdict`, `reasons`,
  `differences` and, for `needs_approval`, `candidate_path` and
  `approve_command`.

## For `needs_approval` contracts

For each one, show the human the candidate card as plain text. Produce it
with the deterministic describer; do not summarise the JSON yourself:

```bash
orchestrator/.venv/bin/python -m recon describe-card --card <candidate_path>
```

Also give them the contract path, so they can check it against the source.
Ask them (AskUserQuestion) whether they approve each card, and ask for the
name to record as the approver. For each card they approve, run its
`approve_command` with their name substituted for `<your name>`. The command
refuses any card that fails a gate.

## For `hold` contracts

Show the human the `reasons` and `differences` verbatim, the paths to both
extractions, and the contract. A hold cannot be approved from here. The
honest outcomes are to stop, or to fix the cause outside this run (for
example, approve a corrected snapshot deliberately) and re-check.

## Record the decision

- If every contract is now approved (or the human fixed the cause and wants
  a re-check):
  `flowstate set-var rates_decision recheck --run-dir {_run_artefact_dir}`
- Otherwise:
  `flowstate set-var rates_decision stop --run-dir {_run_artefact_dir}`

Log the decision with `flowstate event --kind rates_decision --node
rates_hold --message "<who decided what, for which carriers>"`, then
`flowstate complete rates_hold --run-dir {_run_artefact_dir}` and
`advance`. The next node re-runs the gate against the store. Stopping ends
the run at `halted`, and nothing is published.
