"""Canonical records and deterministic JSON I/O.

Every parser emits the same two record shapes, so nothing downstream knows
which carrier format a line came from.

Document record (one per billing document)::

    doc_id, doc_type ("invoice" | "credit_note"), carrier, source_file,
    sha256, format, period_start, period_end, doc_date, printed_total,
    printed_discount, printed_line_count, references_invoice

Line record (one per billed line)::

    line_key ("<doc_id>#<position>"), doc_id, doc_type, carrier, position,
    consignment_ref, billed_total, billed_components [{label, amount}],
    printed {attribute: value as printed}, references_invoice, reason,
    source {file, line}

Amounts are stored as decimal strings; dates as ISO ``YYYY-MM-DD`` strings.
Fields that a format does not carry are ``None``.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from recon.money import to_str

DOC_TYPES = ("invoice", "credit_note")

DOCUMENT_FIELDS = (
    "doc_id", "doc_type", "carrier", "source_file", "sha256", "format",
    "period_start", "period_end", "doc_date", "printed_total",
    "printed_discount", "printed_line_count", "references_invoice",
)

LINE_FIELDS = (
    "line_key", "doc_id", "doc_type", "carrier", "position", "consignment_ref",
    "billed_total", "billed_components", "printed", "references_invoice",
    "reason", "source",
)


def make_document(**fields: Any) -> dict[str, Any]:
    unknown = set(fields) - set(DOCUMENT_FIELDS)
    if unknown:
        raise ValueError(f"unknown document fields: {sorted(unknown)}")
    doc = {name: fields.get(name) for name in DOCUMENT_FIELDS}
    _require(doc, ("doc_id", "doc_type", "carrier", "source_file", "format"), "document")
    if doc["doc_type"] not in DOC_TYPES:
        raise ValueError(f"bad doc_type {doc['doc_type']!r}")
    return doc


def make_line(**fields: Any) -> dict[str, Any]:
    unknown = set(fields) - set(LINE_FIELDS)
    if unknown:
        raise ValueError(f"unknown line fields: {sorted(unknown)}")
    line = {name: fields.get(name) for name in LINE_FIELDS}
    line["billed_components"] = line["billed_components"] or []
    line["printed"] = line["printed"] or {}
    if line["line_key"] is None and line["doc_id"] is not None and line["position"] is not None:
        line["line_key"] = f"{line['doc_id']}#{line['position']}"
    _require(line, ("line_key", "doc_id", "doc_type", "carrier", "position",
                    "consignment_ref", "billed_total"), "line")
    if line["doc_type"] not in DOC_TYPES:
        raise ValueError(f"bad doc_type {line['doc_type']!r}")
    return line


def _require(record: dict[str, Any], names: Iterable[str], kind: str) -> None:
    missing = [n for n in names if record.get(n) in (None, "")]
    if missing:
        raise ValueError(f"{kind} is missing {missing}: {record}")


class _Encoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return to_str(o)
        if isinstance(o, Path):
            return str(o)
        return super().default(o)


def dumps(obj: Any) -> str:
    """Deterministic JSON: sorted keys, Decimals as two-decimal strings."""
    return json.dumps(obj, cls=_Encoder, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_json(path: Path | str, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(obj), encoding="utf-8")


def read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl(path: Path | str, records: Iterable[Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, cls=_Encoder, sort_keys=True, ensure_ascii=False) + "\n")


def read_jsonl(path: Path | str) -> list[Any]:
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
