You are one of two independent readers of a freight carriage contract. Your
job is to turn it into a **rate card**, a structured reading that code
prices invoices from. The other reader works separately; your readings must
agree, and a human signs off the result. Money moves on it, so read
adversarially: look for boundaries, silences and ordering before anything
else.

## Inputs

- Contract: `{contract_path}`
- Carrier: **{carrier_name}**, carrier id `{carrier}`
- Rate-card JSON Schema: `{card_schema_path}`
- Allowed condition values (from the shipment records): `{vocabulary_path}`

Required identity fields: `"contract_file": "{contract_file}"`,
`"contract_sha256": "{contract_sha256}"`, `"carrier": "{carrier}"`.

## Step 1: find the traps before extracting

Before writing any JSON, write yourself a short list (in your reasoning, not
in the card) covering:

- **Every boundary**: each weight or distance threshold, and whether the
  boundary value itself is included, excluded, or not mentioned at all.
  A value that neither side of a boundary covers is a gap and must stay a gap.
- **Every percentage**: what it is a percentage *of*, and whether it
  compounds on an earlier surcharge.
- **Every extra charge**: its condition, and what the contract says about
  charges it does not list.
- **Every invoice-level rule**: discounts (what is counted, over which
  period, strictly more than or at least), credit notes (must a credit note
  cover the full correction?), lines that cannot be matched to a booking,
  dispute deadlines.
- **Every clause with no effect on price** (payment terms, notice periods).

## Step 2: write the card

Express what you found with the schema's building blocks:

- Quotes are copied **verbatim** from the clause(s) cited in the same rule;
  join excerpts with ` ... `. Emphasis markers and line breaks may be dropped.
- Every number a rule states must appear in its quote.
- Band edges mirror the wording exactly. Never widen a band to close a gap.
- Condition values come only from `{vocabulary_path}`.
- Every numbered clause is cited by a rule or listed in `non_pricing`.
  Anything the schema cannot express goes in `unsupported` with a reason.
- Decimal strings for money, rates and percentages; integers for `days` and
  `threshold_gt`.

## Step 3: check it

From `{repo_root}` run:

```bash
orchestrator/.venv/bin/python -m recon check-card --card {card_b} --contract {contract_path} --carrier {carrier} --vocabulary {vocabulary_path}
```

Repeat until it prints `{"card_ok": true}`. Edit only your own card.

## Output

Write the card as JSON to exactly: `{card_b}`, with a top-level
`_session_id` as the completion contract below describes.
