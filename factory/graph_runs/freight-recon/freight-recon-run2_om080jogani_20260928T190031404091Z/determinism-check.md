# Cross-run determinism check

Command: `orchestrator/.venv/bin/python -m recon diff-runs factory/graph_runs/freight-recon/freight-recon_om080jogani_20260928T183534379651Z/out factory/graph_runs/freight-recon/freight-recon-run2_om080jogani_20260928T190031404091Z/out`

Result: `{"identical": true}`

Compared: line and finding membership, billed/expected/delta, dispositions, contract clauses, invoice totals, summary, memo file set. Not compared: justification and memo wording (agent text).

Run 1 needed a human approval of the rate cards (no snapshots existed). Run 2's fresh extractions matched the approved snapshots and passed the rate-card gate with no human step.
