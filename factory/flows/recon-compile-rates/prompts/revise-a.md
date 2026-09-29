You compiled a rate card for the {carrier_name} contract, and an independent
reader compiled another. A code gate then checked both. Either a card failed
a check, or the two cards read the contract differently. You get one
revision.

## Inputs

- Contract: `{contract_path}`
- Your card: `{card_a}`
- The gate report: `{compare_report}`. Read `problems_a` (your card),
  `problems_b` (the other card) and `differences` (where the two readings
  differ, as paths and values).
- Schema: `{card_schema_path}`; allowed condition values: `{vocabulary_path}`

## What to do

For every problem listed for your card, fix it.

For every difference, go back to the contract text of the clause involved
and decide which reading the **text** supports. Change your card only where
the text supports the other reading. Do not adopt the other reader's value
to make the cards agree. If the contract is genuinely silent or ambiguous,
represent that faithfully: leave a gap as a gap, or put the clause in
`unsupported` with the reason. A human then decides. Agreement that the text
does not support is worse than disagreement.

The same rules as before apply: verbatim quotes from the cited clause, every
number in its quote, band edges exactly as worded, vocabulary values only,
every clause accounted for.

Check your revised card from `{repo_root}`:

```bash
orchestrator/.venv/bin/python -m recon check-card --card {repo_root}/{_run_artefact_dir}/card-a-revised.json --contract {contract_path} --carrier {carrier} --vocabulary {vocabulary_path}
```

## Output

Write your complete revised card (not a diff) to exactly: `{repo_root}/{_run_artefact_dir}/card-a-revised.json`,
with a top-level `_session_id` as the completion contract below describes.
