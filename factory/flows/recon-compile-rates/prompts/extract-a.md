You are compiling one freight carriage contract into a **rate card**: a
structured, machine-checkable reading of what the contract says anything
should cost. BlueFin pays carriers from this card, so a wrong reading moves
real money. Another agent is reading the same contract independently, and
the two readings must agree.

## Inputs

- Contract: `{contract_path}` (read the whole file)
- Carrier: **{carrier_name}**, carrier id `{carrier}`
- Rate-card JSON Schema: `{card_schema_path}` (read it; your card must conform)
- Allowed condition values: `{vocabulary_path}` (read it)

Set these fields exactly:
- `"contract_file": "{contract_file}"`
- `"contract_sha256": "{contract_sha256}"`
- `"carrier": "{carrier}"`

## Method

Work through the numbered clauses in order. For each one, decide which
building block it fills, or whether it has no effect on price (then list it
in `non_pricing` with a one-line summary).

## Rules (all of them are checked by code)

1. **Quote verbatim.** Every rule's `quote` must be text copied exactly from
   the clause(s) its `clause` field cites (for example `"§3"` or `"§2, §3"`).
   Markdown emphasis and line breaks may be dropped. To join two excerpts of
   the cited clause(s), put ` ... ` between them, in the order they appear.
2. **Every number in a rule appears in its quote.** Band edges, rates,
   percentages, amounts, day counts and thresholds all count.
3. **Never fill a gap.** Band edges must be exactly as worded: "up to and
   including 500 kg" is `max_kg "500", max_inclusive true`; "under 50 kg" is
   `max_kg "50", max_inclusive false`; "over 50 kg" is `min_kg "50",
   min_inclusive false`. If the contract prices "under X" and "over X", X is
   in neither band. Leave it that way; do not decide what the parties meant.
   The pricing engine reports such cases as undetermined, which is the
   correct outcome.
4. **Use the vocabulary.** Conditions (`when`) and `service_levels` must use
   the exact values in `{vocabulary_path}`, which are the values shipment
   records carry (for example `cold_chain`, not "cold chain").
5. **Surcharge order and base.** List surcharges in the order the contract
   applies them. `base` is `freight` (the freight components only) or
   `freight_and_prior_surcharges` (freight plus the surcharges listed before
   it), whichever the clause text says.
6. **Accessorials.** List each separately payable charge with its condition.
   Set `other_accessorials.allowed` to what the contract says about charges
   it does not list. If it is silent, `false`, citing the clause that defines
   what freight is charged on.
7. **Account for every numbered clause**: cite it from a rule, or list it in
   `non_pricing`. Anything you cannot express with the schema goes in
   `unsupported` with the reason. Do not force it into a rule.
8. Amounts and rates are decimal strings (`"18.00"`, `"12"`); `days` and
   `threshold_gt` are integers.

## Check before you finish

Write the card, then run this from `{repo_root}`:

```bash
orchestrator/.venv/bin/python -m recon check-card --card {repo_root}/{_run_artefact_dir}/card-a.json --contract {contract_path} --carrier {carrier} --vocabulary {vocabulary_path}
```

Fix every problem it reports and run it again until it prints
`{"card_ok": true}`. Do not edit any file other than your card.

## Output

Write the rate card as JSON to exactly: `{repo_root}/{_run_artefact_dir}/card-a.json`

Include a top-level `_session_id` (see the completion contract below).
