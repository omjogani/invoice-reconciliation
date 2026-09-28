"""Sagar Roadlines CSV invoices and credit notes.

Invoices have no id or billing period inside the file, so the document id is
the file stem and the period is left unknown. Credit notes carry their own
id and the invoice they correct on every row. Both end with a ``TOTAL`` row.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from recon.model import make_document, make_line
from recon.money import parse_amount

FORMAT_INVOICE = "sagar-csv-invoice-v1"
FORMAT_CREDIT = "sagar-csv-credit-v1"
FORMAT = FORMAT_INVOICE
CARRIER = "sagar"
INVOICE_HEADER = ["cnote_no", "booking_dt", "wt_kg", "dist_km", "freight_rs", "chill_prem_rs", "total_rs"]
CREDIT_HEADER = ["credit_note", "against_invoice", "cnote_no", "credit_rs"]


def _header(text: str) -> list[str]:
    first = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    return [h.strip() for h in first.split(",")]


def detect(path: Path, text: str) -> bool:
    return path.suffix.lower() == ".csv" and _header(text) in (INVOICE_HEADER, CREDIT_HEADER)


def parse(path: Path, text: str, sha256: str) -> tuple[dict, list[dict]]:
    from recon.parsers import ParseError

    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = list(reader)
    header = [h.strip() for h in rows[0]]
    body = [(n, r) for n, r in enumerate(rows[1:], start=2) if any(c.strip() for c in r)]
    totals = [(n, r) for n, r in body if r[0].strip() == "TOTAL"]
    if len(totals) != 1:
        raise ParseError(f"{path.name}: expected exactly one TOTAL row, found {len(totals)}")
    if totals[0] != body[-1]:
        raise ParseError(f"{path.name}: TOTAL row is not the last row")
    data_rows = body[:-1]
    for n, r in data_rows:
        if len(r) != len(header):
            raise ParseError(f"{path.name}:{n}: expected {len(header)} columns, got {len(r)}")

    if header == INVOICE_HEADER:
        doc_id = path.stem
        total_row = dict(zip(header, totals[0][1]))
        document = make_document(
            doc_id=doc_id, doc_type="invoice", carrier=CARRIER, source_file=path.name, sha256=sha256,
            format=FORMAT_INVOICE, printed_total=parse_amount(total_row["total_rs"]),
        )
        lines = []
        for position, (n, r) in enumerate(data_rows, start=1):
            row = dict(zip(header, (c.strip() for c in r)))
            lines.append(make_line(
                doc_id=doc_id, doc_type="invoice", carrier=CARRIER, position=position,
                consignment_ref=row["cnote_no"], billed_total=parse_amount(row["total_rs"]),
                billed_components=[{"label": "freight_rs", "kind": "freight", "amount": parse_amount(row["freight_rs"])},
                                   {"label": "chill_prem_rs", "kind": "surcharge",
                                    "amount": parse_amount(row["chill_prem_rs"])}],
                printed={"booking_date": row["booking_dt"], "weight_kg": row["wt_kg"],
                         "distance_km": row["dist_km"]},
                source={"file": path.name, "line": n},
            ))
        return document, lines

    if header == CREDIT_HEADER:
        rows_d = [(n, dict(zip(header, (c.strip() for c in r)))) for n, r in data_rows]
        ids = {row["credit_note"] for _, row in rows_d}
        againsts = {row["against_invoice"] for _, row in rows_d}
        if len(ids) != 1:
            raise ParseError(f"{path.name}: credit note ids differ across rows: {sorted(ids)}")
        doc_id = ids.pop()
        total_row = dict(zip(header, totals[0][1]))
        document = make_document(
            doc_id=doc_id, doc_type="credit_note", carrier=CARRIER, source_file=path.name, sha256=sha256,
            format=FORMAT_CREDIT, printed_total=parse_amount(total_row["credit_rs"]),
            references_invoice=againsts.pop() if len(againsts) == 1 else None,
        )
        lines = [make_line(
            doc_id=doc_id, doc_type="credit_note", carrier=CARRIER, position=position,
            consignment_ref=row["cnote_no"], billed_total=parse_amount(row["credit_rs"]),
            references_invoice=row["against_invoice"], source={"file": path.name, "line": n},
        ) for position, (n, row) in enumerate(rows_d, start=1)]
        return document, lines

    raise ParseError(f"{path.name}: unrecognised header {header}")
