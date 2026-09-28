Some billing documents are in a format no parser recognises. Turn them into
canonical records so they are reconciled like every other document. Code
checks your records and ties each document out against its printed total
before anything uses them.

## Inputs

- Inventory: `{inventory_path}`. The `unknown` list names the files, in
  directory `{invoices_dir}` (relative to the repository root).
- Carrier registry: `{carriers_config}`. Use its `carrier` ids exactly.
- The canonical record shapes are documented at the top of
  `recon/model.py` in the repository. Read it.

## Rules

- Transcribe; do not correct. Record what the document prints, even if it
  looks wrong. Pricing and error detection happen later, in code.
- Amounts are strings with two decimals (`"1234.50"`); credits are negative.
- `doc_type` is `invoice` or `credit_note`. For credit notes, fill
  `references_invoice` on the document and on each line.
- `position` counts lines from 1 in document order. `line_key` is
  `<doc_id>#<position>`.
- Put printed attributes (distance, weight, service level, dates) in
  `printed` using the keys `distance_km`, `weight_kg`, `service_level`,
  `booking_date`. Put each printed charge in `billed_components` with a
  `kind` of `freight`, `surcharge` or `accessorial`.
- `printed_total` is the document's printed total. `printed_discount` is
  any document-level discount, and `printed_line_count` is any printed count
  of lines.
- If a document cannot be transcribed faithfully, leave it out and say so
  in completion.yml `errors`. Do not guess.

## Output

Write JSON to exactly `{fallback_path}`:
`{"_session_id": "...", "documents": [...], "lines": [...]}`.
