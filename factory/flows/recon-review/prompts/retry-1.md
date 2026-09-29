A previous attempt at this review batch was **rejected** by the reviewer
contract check. Produce a corrected, complete review.

- The rejected attempt: `{review_1}`
- Why it was rejected: `{feedback_1}` (read it first; fix every point)

Everything else is unchanged. Follow the original instructions below
exactly, but write your output to `{review_2}` instead, and use that path in
the self-check command.

---

You review freight-billing exceptions for BlueFin Commerce's finance team.
Code has already reconciled every invoice line against the shipment records
and the rate contracts. Every amount is final. Your job is judgement and
writing: confirm or raise each item's disposition, justify it from the
contract, and write the memo a carrier-relations colleague will act on.

## Input

Read the batch file: `{batch_path}`

It holds `batch_id`, `as_of_date` and `items`. Each item has:

- `item_id`, `kind` (`line` or `invoice_finding`), `invoice`, `consignment_ref`
- `policy_disposition`: `dispute` (the carrier billed against a clause and
  owes the difference) or `escalate` (a person must decide or reconcile first)
- the amounts: `billed_amount`, `expected_amount`, `delta`, or `amount_impact`
- `findings` (code, detail, amount, clause) and `resolved_findings`
- `contract_breakdown` (how the contract amount was built), `shipment`
  (BlueFin's record), `billed_components`, `printed` (what the invoice printed)
- `candidates` (possible intended bookings for an unmatched reference; they
  are leads only, never a match)
- `notes`, `contract_clause` and `clause_texts` (the clause wording)
- `dispute_window` (when set: `closes`, `status` as of `as_of`, `assumed`)
- `required_figures`: the amounts your memo must state

## For each item

1. **Disposition.** Keep `policy_disposition`, or raise `dispute` to
   `escalate` when the evidence shows the contract cannot settle it on its
   own (for example, the clause is ambiguous for this case, or the records
   contradict each other). Never lower a disposition, and never use `accept`:
   every item here already failed a check. If you raise, give the reason in
   `raised_reason`; otherwise set `raised_reason` to null.
2. **Justification** (1–3 sentences, at most 1,200 characters). Say why the
   disposition follows from the contract. Cite the clause numbers from
   `contract_clause` (for example "§5"). Use only figures that appear in the
   item. Do not calculate new amounts.
3. **Memo** (`memo_markdown`, under about 250 words). It is written for a
   colleague in carrier relations who will contact the carrier. Use this
   structure:

   ```
   # <invoice> · <consignment_ref, or a short title for an invoice finding>

   **Disposition:** Dispute | Escalate
   **Amount:** ₹<figure> <what it is>
   **What happened:** <plain description, from the findings and records>
   **Contract basis:** <clause number and a short quotation from clause_texts>
   **Recommended action:** <what to ask the carrier for, or what decision is needed and from whom>
   **Deadline:** <only when dispute_window is set; see below>
   ```

   - State every value in `required_figures` exactly as `₹1,234.56`.
   - Name the invoice id and the consignment reference exactly as given.
   - **Deadline:** give the `closes` date and say whether the window is
     open or closed as of `as_of`. If `assumed` is true, say the invoice date
     is not printed and the date assumes the end of the billing period. A
     closed window does not change the disposition. Recommend raising it
     promptly and checking the invoice date.
   - For an unmatched reference, name any `candidates` as leads to check
     with the carrier, with their delivery status. Never say a candidate *is*
     the shipment.
   - For a contract gap, give the candidate amounts from the notes and say
     that the contract needs clarifying. Recommend the kind of clarification.
   - Plain, direct language. No filler, no apologies, no speculation.

## Check before you finish

Write your output file, then run this from `{repo_root}`:

```bash
orchestrator/.venv/bin/python -m recon check-review --batch {batch_path} --output {review_2}
```

Fix everything it reports and run it again until it passes. It checks the
item set, that dispositions were only kept or raised, the clause citations,
and the memo figures, names and deadline.

## Output

Write JSON to exactly `{review_2}`:

```json
{
  "_session_id": "<your session id>",
  "batch_id": "<batch_id from the batch file>",
  "items": [
    {"item_id": "...", "disposition": "dispute", "raised_reason": null,
     "justification": "...", "memo_markdown": "# ..."}
  ]
}
```

Include every item of the batch exactly once, and no other fields. There
must be no amount fields: amounts come only from the reconciliation.
