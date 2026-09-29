"""Falcon Freight printed text invoices and credit notes.

Invoice layout::

    TAX INVOICE FALCON-2026-07A    Period: 1-14 2026-07
    ====
    7. Consignment FF-8007
       Mumbai to Bengaluru, 800 km, 1800 kg, standard
       Residential delivery: Rs 250.00
       Freight incl. fuel surcharge: Rs 21,504.00
       LINE TOTAL: Rs 21,754.00
    ====
    INVOICE TOTAL: Rs 181,637.60

Credit notes carry ``CREDIT NOTE <id>    Date: <iso>`` and
``Against: TAX INVOICE <id>``, and free-text correction reasons in place of
the route line. In an invoice every line inside a block must be recognised;
unexpected text is a parse error so format drift is caught, not guessed at.
"""
from __future__ import annotations

import calendar
import re
from pathlib import Path

from recon.model import make_document, make_line
from recon.money import parse_amount

FORMAT = "falcon-text-v1"
CARRIER = "falcon"

_INVOICE_HDR = re.compile(r"^TAX INVOICE\s+(?P<id>\S+)\s+Period:\s*(?P<d1>\d{1,2})-(?P<d2>\d{1,2})\s+(?P<ym>\d{4}-\d{2})\s*$")
_CREDIT_HDR = re.compile(r"^CREDIT NOTE\s+(?P<id>\S+)\s+Date:\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$")
_AGAINST = re.compile(r"^Against:\s*TAX INVOICE\s+(?P<id>\S+)\s*$")
_BLOCK = re.compile(r"^(?P<pos>\d+)\.\s+Consignment\s+(?P<ref>\S+)\s*$")
_ROUTE = re.compile(r"^(?P<origin>.+?) to (?P<dest>.+?),\s*(?P<km>[\d.]+) km,\s*(?P<kg>[\d.]+) kg,\s*(?P<service>[A-Za-z]+)\s*$")
_COMPONENT = re.compile(r"^(?P<label>[A-Za-z][^:]*):\s*(?P<amount>-?\s*Rs\s*-?[\d,]+\.\d{2})\s*$")
_LINE_TOTAL = re.compile(r"^LINE TOTAL:\s*(?P<amount>-?\s*Rs\s*-?[\d,]+\.\d{2})\s*$")
_DOC_TOTAL = re.compile(r"^(?:INVOICE|CREDIT NOTE) TOTAL:\s*(?P<amount>-?\s*Rs\s*-?[\d,]+\.\d{2})\s*$")
_RULE = re.compile(r"^=+$")


def detect(path: Path, text: str) -> bool:
    if path.suffix.lower() != ".txt":
        return False
    head = text[:400]
    return "FALCON FREIGHT PVT LTD" in head and ("TAX INVOICE" in head or "CREDIT NOTE" in head)


def parse(path: Path, text: str, sha256: str) -> tuple[dict, list[dict]]:
    from recon.parsers import ParseError

    rows = text.splitlines()
    doc_id = doc_type = period_start = period_end = doc_date = against = None
    printed_total = None
    blocks: list[dict] = []
    current: dict | None = None

    def close_block() -> None:
        nonlocal current
        if current is not None:
            if current["total"] is None:
                raise ParseError(f"{path.name}:{current['lineno']}: consignment {current['ref']} has no LINE TOTAL")
            blocks.append(current)
            current = None

    for lineno, raw in enumerate(rows, start=1):
        line = raw.strip()
        if not line:
            continue
        if m := _INVOICE_HDR.match(line):
            doc_id, doc_type = m["id"], "invoice"
            year, month = (int(x) for x in m["ym"].split("-"))
            d1, d2 = int(m["d1"]), int(m["d2"])
            last = calendar.monthrange(year, month)[1]
            if not (1 <= d1 <= d2 <= last):
                raise ParseError(f"{path.name}:{lineno}: bad period {line!r}")
            period_start, period_end = f"{year:04d}-{month:02d}-{d1:02d}", f"{year:04d}-{month:02d}-{d2:02d}"
            continue
        if m := _CREDIT_HDR.match(line):
            doc_id, doc_type, doc_date = m["id"], "credit_note", m["date"]
            continue
        if m := _AGAINST.match(line):
            against = m["id"]
            continue
        if m := _BLOCK.match(line):
            close_block()
            current = {"pos": int(m["pos"]), "ref": m["ref"], "lineno": lineno, "route": None,
                       "components": [], "total": None, "reason": []}
            continue
        if m := _DOC_TOTAL.match(line):
            close_block()
            printed_total = parse_amount(m["amount"].replace(" ", ""))
            continue
        if _RULE.match(line):
            continue
        if current is None:
            continue  # letterhead, agreement reference, payment terms
        if m := _LINE_TOTAL.match(line):
            current["total"] = parse_amount(m["amount"].replace(" ", ""))
            continue
        if m := _ROUTE.match(line):
            current["route"] = m.groupdict()
            continue
        if m := _COMPONENT.match(line):
            label = m["label"].strip()
            # Falcon prints freight (with fuel folded in) as one "Freight ..." line;
            # anything else on a consignment is an accessorial charge.
            kind = "freight" if "freight" in label.lower() else "accessorial"
            current["components"].append({"label": label, "kind": kind,
                                          "amount": parse_amount(m["amount"].replace(" ", ""))})
            continue
        if doc_type == "credit_note":
            current["reason"].append(line)
            continue
        raise ParseError(f"{path.name}:{lineno}: unrecognised invoice text {line!r}")
    close_block()

    if doc_id is None or doc_type is None:
        raise ParseError(f"{path.name}: no TAX INVOICE or CREDIT NOTE header")
    if printed_total is None:
        raise ParseError(f"{path.name}: no document total")
    if doc_type == "credit_note" and against is None:
        raise ParseError(f"{path.name}: credit note without 'Against:' reference")
    if doc_type == "invoice" and any(b["route"] is None for b in blocks):
        bad = [b["ref"] for b in blocks if b["route"] is None]
        raise ParseError(f"{path.name}: consignments without a route line: {bad}")
    positions = [b["pos"] for b in blocks]
    if positions != list(range(1, len(blocks) + 1)):
        raise ParseError(f"{path.name}: line numbering is not 1..n: {positions}")

    document = make_document(
        doc_id=doc_id, doc_type=doc_type, carrier=CARRIER, source_file=path.name, sha256=sha256,
        format=FORMAT, period_start=period_start, period_end=period_end, doc_date=doc_date,
        printed_total=printed_total, printed_discount=None, printed_line_count=None,
        references_invoice=against,
    )
    lines = []
    for b in blocks:
        printed = {}
        if b["route"]:
            r = b["route"]
            printed = {"origin": r["origin"], "destination": r["dest"], "distance_km": r["km"],
                       "weight_kg": r["kg"], "service_level": r["service"].lower()}
        lines.append(make_line(
            doc_id=doc_id, doc_type=doc_type, carrier=CARRIER, position=b["pos"],
            consignment_ref=b["ref"], billed_total=b["total"], billed_components=b["components"],
            printed=printed, references_invoice=against,
            reason=" ".join(b["reason"]) or None,
            source={"file": path.name, "line": b["lineno"]},
        ))
    return document, lines
