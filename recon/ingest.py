"""Inventory, parse and tie out every billing document.

Outputs (all deterministic, sorted by file name):

- ``documents.json``  document records for every parsed document
- ``lines.jsonl``     canonical lines, document order then position
- ``inventory.json``  every file with its fingerprint, detected format and
                      outcome (parsed / unknown), plus tie-out results

A tie-out failure or a malformed known-format file raises ``IngestError``,
which stops the run before pricing. Unknown formats do not raise: they are
listed so the flow can route them to the agent fallback.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from recon.model import write_json, write_jsonl
from recon.money import D, ZERO, to_str
from recon.parsers import ParseError, detect


class IngestError(RuntimeError):
    pass


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tie_out(document: dict, lines: list[dict]) -> dict:
    """Check a document's lines against its printed totals.

    sum(line totals) − printed discount must equal the printed total, and a
    printed line count must equal the number of parsed lines.
    """
    line_sum = sum((D(l["billed_total"]) for l in lines), ZERO)
    discount = D(document["printed_discount"]) if document["printed_discount"] is not None else ZERO
    computed = line_sum - discount
    printed = D(document["printed_total"])
    problems = []
    if computed != printed:
        problems.append(f"lines sum to {to_str(line_sum)} less discount {to_str(discount)} = "
                        f"{to_str(computed)}, printed total is {to_str(printed)}")
    count = document["printed_line_count"]
    if count is not None and count != len(lines):
        problems.append(f"printed line count {count}, parsed {len(lines)}")
    if not lines:
        problems.append("document has no lines")
    return {"doc_id": document["doc_id"], "passed": not problems, "problems": problems,
            "line_sum": line_sum, "discount": discount, "printed_total": printed,
            "line_count": len(lines)}


def ingest(invoice_dir: Path) -> tuple[list[dict], list[dict], dict]:
    invoice_dir = Path(invoice_dir)
    if not invoice_dir.is_dir():
        raise IngestError(f"{invoice_dir} is not a directory")
    files = sorted(p for p in invoice_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        raise IngestError(f"no billing documents in {invoice_dir}")

    documents: list[dict] = []
    all_lines: list[dict] = []
    entries: list[dict] = []
    unknown: list[str] = []
    for path in files:
        raw = path.read_bytes()
        sha = fingerprint(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IngestError(f"{path.name}: not UTF-8 text ({exc})") from exc
        parser = detect(path, text)
        if parser is None:
            entries.append({"file": path.name, "sha256": sha, "format": None, "status": "unknown"})
            unknown.append(path.name)
            continue
        try:
            document, lines = parser.parse(path, text, sha)
        except ParseError as exc:
            raise IngestError(str(exc)) from exc
        result = tie_out(document, lines)
        if not result["passed"]:
            raise IngestError(f"{path.name} ({document['doc_id']}) does not tie out: "
                              + "; ".join(result["problems"]))
        documents.append(document)
        all_lines.extend(lines)
        entries.append({"file": path.name, "sha256": sha, "format": document["format"],
                        "status": "parsed", "doc_id": document["doc_id"], "tie_out": result})

    ids = [d["doc_id"] for d in documents]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise IngestError(f"document ids appear more than once: {dupes}")

    inventory = {"invoice_dir": str(invoice_dir), "files": entries, "unknown": unknown,
                 "document_count": len(documents), "line_count": len(all_lines)}
    return documents, all_lines, inventory


def write_outputs(out_dir: Path, documents: list[dict], lines: list[dict], inventory: dict) -> None:
    out_dir = Path(out_dir)
    write_json(out_dir / "documents.json", documents)
    write_jsonl(out_dir / "lines.jsonl", lines)
    write_json(out_dir / "inventory.json", inventory)
