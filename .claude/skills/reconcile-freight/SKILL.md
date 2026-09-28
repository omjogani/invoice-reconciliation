---
name: reconcile-freight
description: Run BlueFin's freight billing reconciliation end to end. Drives the freight-recon flowstate graph (deterministic reconciliation, agent contract compilation and exception review, gates on every hand-off) and publishes reconciliation-report.json and memos/. Use when asked to reconcile carrier invoices or re-run the reconciliation.
---

# Reconcile freight billing

This skill starts a run of `factory/flows/freight-recon` and then hands you
to the **graph-orchestrator** skill, which you follow for everything else.
Read `.claude/skills/graph-orchestrator/SKILL.md` now if you have not
already, especially the failure policy and the `dynamic_fanout` procedure.

## What the flow does

`ingest` → (`parse_fallback` → `merge_fallback`, only for unknown formats) →
`plan_compile` → **compile fan-out** (child flow `recon-compile-rates`, one per
contract: two agent extractions, agreement gate, one revision round) →
`rates_check` (hash-keyed approved snapshots) → [`rates_hold` →
`rates_recheck`, only when a human must approve] → `reconcile` →
`plan_review` → **review fan-out** (child flow `recon-review`, one per batch
of exceptions: agent review, reviewer-contract gate, two retries) →
`review_check` → `assemble` → `validate_report` → `publish` → `done`.

Script nodes run by themselves during `advance`. You spawn workers only for
the agent nodes inside the child runs (and `parse_fallback`, if it runs).

## 1. Preconditions

```bash
test -x orchestrator/.venv/bin/python || orchestrator/setup.sh
command -v tmux claude >/dev/null
scripts/test.sh            # the deterministic core must be green before a run
```

## 2. Bootstrap

Use the session id from the `CLAUDE_CODE_SESSION_ID=` line injected at
session start. The `as_of_date` is the date memos judge dispute deadlines
against. Use today's date unless the user gives one, and tell the user which
date you used.

```bash
orchestrator/bin/flowstate bootstrap \
  --flow-dot factory/flows/freight-recon/freight-recon.dot \
  --run-descriptor freight-recon \
  --supervision afk \
  --orchestrator-session-id "<session id>" \
  --seed-var invoices_dir=data/invoices \
  --seed-var shipments_path=data/shipments.json \
  --seed-var carriers_config=config/carriers.json \
  --seed-var approved_store=rate-cards/approved \
  --seed-var as_of_date=<YYYY-MM-DD> \
  --seed-var publish_dir=.
```

Bootstrap runs `ingest` and `plan_compile` inline and lands on
`compile_fan`. Continue with `advance --node compile_fan` (dynamic fan-out,
step 1 of the procedure in graph-orchestrator).

## 3. Drive the run

Follow graph-orchestrator exactly. Points specific to this flow:

- **Child workers:** spawn with `orchestrator/bin/spawn-node <node> --run-dir
  <child run dir> --session <tmux session from your first spawn>`. Inside a
  compile child, `extract` and `revise` are forks: spawn both arms and wait
  for both before advancing.
- **Rate cards:** when every contract's hash has an approved snapshot in
  `rate-cards/approved/` and the extractions agree with it, `rates_check`
  routes straight to `reconcile`. Otherwise the run reaches `rates_hold`: run
  that node yourself, following its prompt. Only a human approves a card.
- **Script failures are final for the run.** `reconcile`, `review_check`,
  `assemble` and `validate_report` are deterministic. If one fails, stop and
  report its stderr (failure policy). Never write their outputs by hand.
- **Log** every retry, respawn, approval and stop with `flowstate event`.

## 4. Finish

When `advance` returns `kind: end`:

1. Check which end node was reached (`flowstate status`). `done` means the
   report and memos were validated and published at the repository root;
   `halted` means nothing was published.
2. Kill the run's tmux sessions (graph-orchestrator step 3).
3. Report to the user: the summary block of `reconciliation-report.json`,
   the number of memos, the run directory, and `run-manifest.json`.
4. If an earlier completed run exists, compare the two and report the result:
   `orchestrator/.venv/bin/python -m recon diff-runs <earlier run>/out <this run>/out`
