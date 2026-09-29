"""Alpine Express Logistics JSON invoices.

Alpine prints its own chargeable weight, rate and handling fee per line but
not a separate freight amount. The parser records freight as a *derived*
component (printed chargeable weight × printed rate, rounded half-up) so the
generic arithmetic check can compare the line's components with its total.
These printed figures are the carrier's claims; pricing never uses them.
"""
from __future__ import annotations

import calendar
import json
import re
from pathlib import Path

from recon.model import make_document, make_line
from recon.money import D, q2

FORMAT = "alpine-json-v1"
CARRIER = "alpine"
_PERIOD = re.compile(r"^\d{4}-\d{2}$")
_REQUIRED_LINE_KEYS = {"sl", "consignment_no", "booking_date", "actual_weight_kg",
                       "chargeable_weight_kg", "rate_per_kg", "handling_fee", "line_amount"}


def detect(path: Path, text: str) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    return (isinstance(data, dict) and data.get("carrier") == "Alpine Express Logistics"
            and "invoice_no" in data and isinstance(data.get("lines"), list))


def parse(path: Path, text: str, sha256: str) -> tuple[dict, list[dict]]:
    from recon.parsers import ParseError

    data = json.loads(text)
    for key in ("invoice_no", "billing_period", "lines", "invoice_total"):
        if key not in data:
            raise ParseError(f"{path.name}: missing {key!r}")
    period = data["billing_period"]
    if not _PERIOD.match(str(period)):
        raise ParseError(f"{path.name}: billing_period {period!r} is not YYYY-MM")
    year, month = (int(x) for x in period.split("-"))
    last = calendar.monthrange(year, month)[1]
    doc_id = data["invoice_no"]

    document = make_document(
        doc_id=doc_id, doc_type="invoice", carrier=CARRIER, source_file=path.name, sha256=sha256,
        format=FORMAT, period_start=f"{period}-01", period_end=f"{period}-{last:02d}", doc_date=None,
        printed_total=D(data["invoice_total"]), printed_discount=D(data.get("discount", 0)),
        printed_line_count=data.get("consignment_count"), references_invoice=None,
    )
    lines = []
    for index, row in enumerate(data["lines"]):
        missing = _REQUIRED_LINE_KEYS - set(row)
        if missing:
            raise ParseError(f"{path.name}: line {index + 1} missing {sorted(missing)}")
        position = index + 1
        if row["sl"] != position:
            raise ParseError(f"{path.name}: line {position} has sl={row['sl']}")
        chargeable, rate, handling = D(row["chargeable_weight_kg"]), D(row["rate_per_kg"]), D(row["handling_fee"])
        components = [{"label": "freight (chargeable_weight_kg x rate_per_kg)",
                       "kind": "freight", "amount": q2(chargeable * rate), "derived": True}]
        if handling != 0:
            components.append({"label": "handling_fee", "kind": "accessorial", "amount": handling})
        lines.append(make_line(
            doc_id=doc_id, doc_type="invoice", carrier=CARRIER, position=position,
            consignment_ref=row["consignment_no"], billed_total=D(row["line_amount"]),
            billed_components=components,
            printed={"booking_date": row["booking_date"], "weight_kg": str(row["actual_weight_kg"]),
                     "chargeable_weight_kg": str(row["chargeable_weight_kg"]),
                     "rate_per_kg": str(row["rate_per_kg"]), "handling_fee": str(row["handling_fee"])},
            source={"file": path.name, "pointer": f"/lines/{index}"},
        ))
    return document, lines
