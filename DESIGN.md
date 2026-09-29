# DESIGN: freight billing reconciliation

BlueFin pays carriers from this output, so it has to hold up on every run,
not just a good one. That requirement decided most of what follows.

**Result of the submitted run** (`reconciliation-report.json`, `memos/`):
380 lines across 17 documents. 364 accept, 12 dispute, 4 escalate, plus one
invoice-level finding. ₹16,618.06 in dispute and 17 memos. A second,
independent run produced an identical report (see *Evidence*).

## 1. Architecture, and why

A **hybrid on the flowstate graph**. Code owns every rupee; agents own the
reading and the writing; a gate sits on every hand-off.

```
freight-recon (parent)
  ingest ─► [parse_fallback ─► merge_fallback]* ─► plan_compile
    ─► compile fan-out ── recon-compile-rates (one child per contract)
    │      fork: extract_a ∥ extract_b ─► compare gate ─► [revise_a ∥ revise_b ─► compare gate] ─► finalize
    ─► rates_check ─► [rates_hold (human) ─► rates_recheck]* ─► reconcile ─► plan_review
    ─► review fan-out ─── recon-review (one child per batch of exceptions)
    │      review ─► check gate ─► [retry ─► check]×≤2 ─► finalize
    ─► review_check ─► assemble ─► validate_report ─► publish ─► done      (or ─► halted)
  * only when needed
```

| Work | Done by | Why |
|---|---|---|
| Parsing three formats, tie-out, matching, pricing, duplicates, credits, discounts, deadlines, dispositions, totals | Python (`recon/`), run as flow **script nodes** | Must be identical on every run. Script nodes run inline during `advance`, so they cost nothing to orchestrate. |
| Turning prose contracts into rate cards | **Agents**: two independent extractions per contract | Reading prose is where an LLM adds value, and a mistake there is caught by code (§2). |
| Reviewing each exception, writing its justification and memo | **Agents**, in batches of ≤12 items | Judgement and clear writing; cost grows with exceptions (~5% of lines), not with volume. |
| Approving a rate card for a new contract version | **A human** | A rate card is a standing instruction to pay; no agent can grant it. |
| Operating the run | **Skills**: `reconcile-freight` (entry), `graph-orchestrator` (protocol and failure policy) | Procedure only. No skill decides an amount or a disposition. |

Options considered and rejected:
- **One agent with a skill.** It would do the arithmetic itself, and a 380-line context invites the "lost in the middle" failures `brief.md` warns about.
- **A skill chain.** Steps could still be skipped.
- **Pure scripts.** Not an agent system, and it would break on a new format or contract.
- **All-agent flowstate.** A schema can't catch a wrong number.
- **Agent-generated code.** A different program on every run.

**Rate cards (decision D1).** A card expresses a contract with a closed
set of building blocks: weight bands with explicit edge inclusivity, a
chargeable-weight minimum, ordered surcharges with a base, conditional
accessorials, volume discounts, and rules for credit notes, unmatched
lines and dispute windows. Every rule cites its clause and quotes it word
for word. A case the contract leaves open is left open, never filled
(Alpine's exactly-50 kg). Two agents compile each contract independently.
Code checks both cards and compares them, and the agreed reading must
equal a **human-approved snapshot keyed by the contract's SHA-256**. When
it matches, pricing uses the *snapshot*, so clause citations are identical
on every run. A changed contract has a new hash, no snapshot, and the run
pauses for a human. The snapshots in `rate-cards/approved/` were approved
in run 1 by Om Jogani, after the orchestrator checked each rule against its
clause and against an independent hand reading.

## 2. What is validated, where, and why there

The rule: check each thing at the earliest point it can be known, in code,
so a bad value never reaches a later stage.

| Where | What | Why here |
|---|---|---|
| `ingest` | Every file fingerprinted. Known formats parsed strictly (unrecognised invoice text is an error, not a guess). Each document ties out: line sum less discount = printed total, printed line count = parsed count. Duplicate document ids rejected. | Parsing errors must never surface later as pricing "disputes". |
| `merge_fallback` | Agent-parsed records validated and tied out exactly like parser output. | The fallback agent is trusted no more than a parser. |
| Agent self-checks (`check-card`, `check-review`) | Agents run the same gates on their own output before finishing. | Catches most mistakes without a retry. It adds no trust: the gates below re-run independently. |
| `compare` / `compare_2` (compile child) | Schema; contract hash, file and carrier; cited clauses exist; **each quote is verbatim inside the clause it cites**; **every number in a rule appears in its quote**; bands never overlap; every clause accounted for; conditions use the shipment vocabulary; the two cards read the contract the same way. One revision round, in which agents may change their card only where the contract text supports it. | This is where LLM variance enters. The quote and number checks catch invented or misplaced figures; agreement catches random misreadings. |
| `rates_check` | The same checks again, plus comparison against the approved snapshot. `ok` / `needs_approval` / `hold`; never picks between readings. | A misreading that both agents share is caught by the snapshot. |
| `reconcile` | Every line priced or explicitly undetermined. An accepted line with a positive delta raises an error. | No line reaches policy unpriced by accident. |
| `check_1..3` (review child) | Same item set; dispositions only kept or raised (with a reason); no amount fields; justification cites a governing clause; memo names the invoice and consignment and states the exact amounts and the dispute-window date. Two retries with the gate's feedback. | Agent output is untrusted until checked. The retries are graph nodes, so they appear in the run state. |
| `review_check` | Every batch settled and passing, re-checked from scratch. | Defence in depth before assembly. |
| `validate_report` | `report.schema.json`; every parsed line **exactly once** in order; delta = billed − expected, null exactly when expected is null; no accepted line without a price or with a positive delta; invoice and summary totals re-derived; **each disputed rupee counted once**; one memo per non-accept item with the right figures. Implemented independently of `assemble`, so an assembly bug cannot validate itself. | The last check before publishing. The flow cannot reach `done` without passing it. |
| `recon diff-runs` | Two runs identical in membership, amounts, dispositions, clauses, totals and memo set. | Proves run-to-run stability. |

Unit tests: 105 (`scripts/test.sh`). They cover parsers, gates, pricing
edges, every reconciliation rule, review checks, tampering against the
validator, and flow definitions (schemas copied into the flows stay in sync
with `recon`). The test fixture rate cards under `tests/fixtures/` are
hand-written for tests only; the flow never reads them.

## 3. Judgement calls in the reconciliation

| # | Question | Decision |
|---|---|---|
| D1 | Where pricing rules come from | Agent-compiled rate cards, gated, matched to a hash-keyed human-approved snapshot (§1). |
| D2 | Alpine at exactly 50 kg (§2 prices "under 50" and "over 50") | A contract gap. `expected_amount` null, escalated, with both candidates (₹475.00 at ₹9.50, ₹412.50 at ₹8.25) in notes and memo. This makes those invoices' `expected_total`, and `summary.total_expected`, null, which the schema allows for exactly this case. Invoice notes give the determinable subtotal and the range. Paying either rate would be a guess. |
| D3 | Credit notes | Credits are applied to the original line: expected = contract amount + credits issued; a valid credit line is accepted at its billed amount. FF-8005's credit fully corrects it (accept). SG-7056 was credited ₹156 of a ₹260 overbill, and Sagar §6 requires full cover, so ₹104 is disputed. Each rupee is counted once. |
| D4 | Duplicate billing | Keep the copy whose billing period contains the ship date. The other copy is disputed in full at expected ₹0, with the shipment id still matched (FF-8003 on 07B, FF-8055 on 09A: ₹12,230.40). If the copies' details differ, escalate instead. |
| D5 | References with no booking | Matching is exact only. SG-7969 and SG-7922 are escalated (Sagar §5). The near misses SG-7069 and SG-7122 (same date, weight and distance, one character off, **in transit**) appear in memos as leads, never as matches. |
| D6 | Alpine volume discount | Counted from consignments *tendered* (shipments), strictly more than 12. The base is the **expected** subtotal, since billed would double-count line overbilling. ALPINE-0726: ₹1,007.46 disputed on determinable lines; AE-3005's share stays with its escalation. |
| D7 | How far the review agent is trusted | It may only raise a disposition, never lower one or change an amount. Enforced by code. |
| D8 | Falcon's 45-day dispute window | Notes only, never a change of disposition. Absolute dates, with the assumed invoice date stated (end of billing period; not printed). Memos judge open or closed against a seeded `as_of_date`, so results never depend on the day the report is run. |
| D9 | Workers | `sonnet`; `autonomy="full"` only on agent nodes that run the self-check commands; `afk` supervision. |

Smaller calls:
- Money is exact Decimal, rounded half-up once per line. That reproduces the carriers' own rounding (589.875 → 589.88).
- Delta tolerance is zero.
- Printed attributes that differ from the shipment record are noted but not a disposition by themselves; the price always comes from the record.
- Unauthorised extra charges are identified by pairing billed extras with contract charges, so FF-8078's legitimate ₹250 is not confused with its fuel overcharge.
- Invoice notes show "net payable now / held", following Falcon §7: undisputed items remain due.

## 4. Designing for much larger volumes

- **Agent cost grows with contracts and exceptions, not lines.** Clean lines never reach a model. Review batches are capped at 12 items of one carrier, and a contract is compiled per version (the snapshot is reused until the hash changes).
- **Fan-out with bounded context.** One child run per contract and per batch, with `max_concurrent`. No worker's context grows with the run.
- **New carriers are data.** Add an entry in `config/carriers.json`, a contract, and a parser module (or let the gated fallback agent handle a new format). A clause the rate-card building blocks cannot express is reported as `unsupported` and stops for a human: extending the engine is a deliberate code change, never an improvisation.
- **The core is linear.** Indexed matching, near-miss lookup and duplicate grouping; per-document parsing is independent and cacheable by fingerprint.
- **Not built, designed for:** a persistent ledger of disputes and billed consignments across runs (credits and duplicates that arrive in a later month); real invoice dates from accounts payable in place of the D8 assumption; splitting the `reconcile` step per carrier if it approaches the engine's 120-second script limit.

## 5. Changes to the provided machinery

No flowstate or agentctl engine code was changed.

| Change | Why |
|---|---|
| Execute bits on `orchestrator/bin/*`, `setup.sh`, the SessionStart hook and the demo scripts (commit `52b1191`) | They were committed as `100644`. On a fresh clone the CLIs, the hook and every script node fail. |
| New `orchestrator/bin/spawn-node` (+ `lib/spawn_node.py`) | Reads the node config and prompt with the venv's YAML parser, **refuses an empty or unrendered prompt**, spawns, and records every spawn in `spawn-ledger.jsonl`. `--retry-feedback` respawns with the validator's feedback and moves the rejected `completion.yml` aside. |
| `graph-orchestrator` SKILL.md: a mandatory **failure policy** (validation retry ×2, stall and death handling, blocked gates, script errors, human-only hold nodes, an event logged for every decision) and a verified procedure for driving `dynamic_fanout` children | The kit's skill explicitly left these undefined. |
| New `reconcile-freight` skill | The one-command entry point. |

## 6. Post-implementation notes

Things learned by building and running this. Most are engine behaviours
that shaped the design:

1. **A failed validation leaves no worker to fix it.** The kit's skill says the worker is "left alive" to fix its output. In fact the wrapper ends a worker as soon as its `completion.yml` appears. Retries have to be respawns, which is why `spawn-node --retry-feedback` exists. Verified on smoke-test: a corrupted output was rejected, the retry fixed it, and the run completed.
2. **A join wired straight into an end node crashes the advance envelope.** The end node is treated as an agent node with no prompt template. Found in a spike; every join here is followed by a script node.
3. **An agent fork arm cannot reference its own output path in its prompt.** Its output-path variables are filled in only at validation, and `render-prompt` reads the trunk scope. Found in the first real run: `spawn-node` refused to spawn rather than send a broken prompt. The prompts now build the path from existing variables (commit `f5a0f87`).
4. **Cycles are treated as suspect.** Retries and revision rounds are written out as explicit nodes, which also makes every attempt visible in the run state.
5. **Variables are capped at 16 KB**, including a `dynamic_fanout` source list. Items are kept to about 200 bytes (≈80 batches ≈ 1,000 exceptions per run). Beyond that, the next step would be nested fan-out or an engine change.
6. **`start-branch` does not enforce `max_concurrent`.** The orchestrator must start only `next_startable_branches`, and the skill says so.
7. **`permission_mode=` in the demo DOT files is ignored**; `autonomy=` is the current attribute.
8. **`autonomy="full"` shows a "Bypass Permissions" consent dialog** until a person accepts it on that machine. In run 1 the workers waited on it. The acceptance was the user's to give (the orchestrator was, correctly, not allowed to accept it), and later spawns ran without it. A fresh machine will show it again. The alternative is least privilege: default mode plus a narrow allowlist for the two self-check commands. It avoids the dialog but was not adopted for these runs.
9. **The first real run found a bug in my agreement gate.** On the Sagar card, the two extractions differed only in inclusivity flags on bands with *no* edge, which mean nothing. The gate sent the child to a revision round. It is fixed and tested (commit `63a6311`), and run 2 agreed on the first round. It was harmless (a spurious revision, never a wrong amount), and it shows the gates being tested against real agent output.
10. **The system `python3` lacks PyYAML.** During exploration, a prompt piped through it came out empty and a worker was spawned with no instructions. `spawn-node` makes that impossible.

## 7. Evidence

| What | Where |
|---|---|
| Run 1: human rate-card approval, published first | `factory/graph_runs/freight-recon/freight-recon_om080jogani_20260928T183534379651Z/` |
| Run 2: no human step, the submitted outputs | `factory/graph_runs/freight-recon/freight-recon-run2_om080jogani_20260928T190031404091Z/` |
| Cross-run comparison (`{"identical": true}`) | `…/freight-recon-run2_…/determinism-check.md` |
| Child runs (cards, gate reports, reviews) | `factory/graph_runs/recon-compile-rates/`, `factory/graph_runs/recon-review/` |
| Rendered prompts, spawn records | `factory/execution/temporary/recon-*/…/prompt.raw.txt`; `spawn-ledger.jsonl` in each child run (the parent spawns no workers) |
| Run state and event log (every phase, validation, retry, approval) | `graph_run_state.yml` in each run directory |
| Approved rate cards with approval metadata | `rate-cards/approved/<carrier>/<contract sha256>.json` |
| Output ↔ run ↔ inputs | `run-manifest.json` (input fingerprints, snapshot paths, git head, `as_of_date`) |

Worker transcripts stay in the operator's `~/.claude/projects/`. Each
phase records its `agent_session_id` so they can be traced. They are not
committed because they contain local session data.

## 8. How to run it

```bash
orchestrator/setup.sh          # once; needs python3, git, tmux, jq, claude
scripts/test.sh                # deterministic core: must be green
claude                         # then: "reconcile the freight invoices" (the reconcile-freight skill)
```

With the committed snapshots and unchanged contracts, the run needs no
human input. Only the first bypass-permissions dialog appears, on a
machine that has never accepted it. It publishes `reconciliation-report.json`,
`memos/` and `run-manifest.json`. Compare any two runs with
`orchestrator/.venv/bin/python -m recon diff-runs <run A>/out <run B>/out`.
