"""Exception review: batches for agents, and the gate on what comes back (D7).

Only items whose policy disposition is not ``accept`` are reviewed. Each
batch holds at most ``batch_size`` items of one carrier, so a reviewer's
context never grows with the size of the run.

A reviewer returns, per item: a disposition, a reason if it raised one, a
justification and a memo. ``check_review`` rejects anything that lowers a
disposition, drops or adds items, carries amounts, fails to cite the clause,
or writes a memo whose figures do not match the report.
"""
from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from recon.money import D, amount_spellings, q2
from recon.policy import RANK
from recon.ratecard.contract import load_contract, parse_clause_refs

REVIEW_OUTPUT_SCHEMA = Path(__file__).with_name("schemas") / "review-output.json"
MAX_MEMO_CHARS = 3000


def memo_file_for(item_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", item_id.replace("#", "__")) + ".md"


def _date_spellings(iso: str) -> set[str]:
    d = date.fromisoformat(iso)
    return {iso, f"{d.day} {d.strftime('%B')} {d.year}", f"{d.day} {d.strftime('%b')} {d.year}",
            f"{d.strftime('%B')} {d.day}, {d.year}", f"{d.strftime('%b')} {d.day}, {d.year}"}


def window_status(window: dict | None, as_of: str) -> dict | None:
    if not window:
        return None
    days_left = (date.fromisoformat(window["closes"]) - date.fromisoformat(as_of)).days
    return {**window, "as_of": as_of, "status": "open" if days_left >= 0 else "closed", "days_left": days_left}


# ----------------------------------------------------------------------------- planning
def _clause_texts(contract_path: Path, clause_ref: str | None) -> dict[str, str]:
    if not clause_ref:
        return {}
    contract = load_contract(contract_path)
    refs = parse_clause_refs(clause_ref)
    return {f"§{n}": " ".join(contract.clauses[n].split()) for n in refs if n in contract.clauses}


def _line_item(rec: dict, doc_summary: dict, contract_path: Path, as_of: str) -> dict:
    figure = rec["delta"] if rec["delta"] is not None else rec["billed_total"]
    pricing = rec.get("pricing") or {}
    return {
        "item_id": rec["line_key"], "kind": "line", "invoice": rec["doc_id"], "carrier": rec["carrier"],
        "consignment_ref": rec["consignment_ref"], "shipment_id": rec["shipment_id"],
        "policy_disposition": rec["disposition"], "billed_amount": rec["billed_total"],
        "expected_amount": rec["expected"], "delta": rec["delta"],
        "findings": [{k: f[k] for k in ("code", "detail", "amount", "clause")} for f in rec["findings"]
                     if not f["resolved_by"]],
        "resolved_findings": [{"code": f["code"], "detail": f["detail"], "resolved_by": f["resolved_by"]}
                              for f in rec["findings"] if f["resolved_by"]],
        "notes": rec["notes"], "billed_components": rec["billed_components"], "printed": rec["printed"],
        "shipment": rec["shipment"],
        "contract_breakdown": None if pricing.get("status") != "priced" else {
            "components": [{k: c[k] for k in ("label", "basis", "band", "rate", "quantity", "amount")}
                           for c in pricing["components"]],
            "surcharges": [{k: s[k] for k in ("label", "pct", "base", "amount")} for s in pricing["surcharges"]],
            "accessorials": [{k: a[k] for k in ("label", "amount")} for a in pricing["accessorials"]],
            "contract_amount": rec["contract_amount"]},
        "candidates": rec["candidates"],
        "contract_clause": rec["contract_clause"],
        "clause_texts": _clause_texts(contract_path, rec["contract_clause"].split(" ", 1)[1]
                                      if rec["contract_clause"] else None),
        "dispute_window": window_status(doc_summary.get("dispute_window"), as_of),
        "required_figures": [q2(figure)],
        "memo_file": memo_file_for(rec["line_key"]),
    }


def _finding_item(f: dict, doc_summary: dict, contract_path: Path, as_of: str) -> dict:
    return {
        "item_id": f["finding_id"], "kind": "invoice_finding", "invoice": f["doc_id"], "carrier": f["carrier"],
        "consignment_ref": None, "policy_disposition": f["disposition"], "code": f["code"],
        "description": f["detail"], "amount_impact": f["amount_impact"], "notes": f["notes"],
        "invoice_notes": doc_summary.get("notes", []), "contract_clause": f["contract_clause"],
        "clause_texts": _clause_texts(contract_path, f["contract_clause"].split(" ", 1)[1]),
        "dispute_window": window_status(doc_summary.get("dispute_window"), as_of),
        "required_figures": [q2(f["amount_impact"])] if f["amount_impact"] is not None else [],
        "memo_file": memo_file_for(f["finding_id"]),
    }


def plan_review(reconciled: dict, contracts: dict[str, Path], as_of: str, batch_size: int = 12) -> list[dict]:
    """Group non-accept items into batches of one carrier each, in a stable order."""
    items: list[dict] = []
    for rec in reconciled["lines"]:
        if rec["disposition"] != "accept":
            items.append(_line_item(rec, reconciled["documents"][rec["doc_id"]], contracts[rec["carrier"]], as_of))
    for f in reconciled["invoice_findings"]:
        if f["disposition"] != "accept":
            items.append(_finding_item(f, reconciled["documents"][f["doc_id"]], contracts[f["carrier"]], as_of))
    batches: list[dict] = []
    for carrier in sorted({i["carrier"] for i in items}):
        mine = [i for i in items if i["carrier"] == carrier]
        for start in range(0, len(mine), batch_size):
            batches.append({"batch_id": f"B{len(batches) + 1:02d}", "carrier": carrier, "as_of_date": as_of,
                            "items": mine[start:start + batch_size]})
    return batches


# ----------------------------------------------------------------------------- checking
@lru_cache(maxsize=1)
def _output_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(REVIEW_OUTPUT_SCHEMA.read_text(encoding="utf-8")))


def check_memo(item: dict, memo: str) -> list[str]:
    problems = []
    iid = item["item_id"]
    if len(memo) > MAX_MEMO_CHARS:
        problems.append(f"{iid}: memo is {len(memo)} characters; keep it under {MAX_MEMO_CHARS}")
    if item["invoice"] not in memo:
        problems.append(f"{iid}: memo does not name invoice {item['invoice']}")
    if item.get("consignment_ref") and item["consignment_ref"] not in memo:
        problems.append(f"{iid}: memo does not name consignment {item['consignment_ref']}")
    for figure in item["required_figures"]:
        if not any(s in memo for s in amount_spellings(D(figure))):
            problems.append(f"{iid}: memo does not state the amount {q2(D(figure)):,.2f}")
    window = item.get("dispute_window")
    if window and not any(s in memo for s in _date_spellings(window["closes"])):
        problems.append(f"{iid}: memo does not state the dispute-window date {window['closes']}")
    return problems


def check_review(batch: dict, output: Any) -> list[str]:
    problems = [f"schema: /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                for e in sorted(_output_validator().iter_errors(output), key=lambda e: list(e.absolute_path))]
    if problems:
        return problems
    if output["batch_id"] != batch["batch_id"]:
        problems.append(f"batch_id {output['batch_id']!r} is not {batch['batch_id']!r}")
    given = {i["item_id"]: i for i in batch["items"]}
    returned = [r["item_id"] for r in output["items"]]
    if len(returned) != len(set(returned)):
        problems.append("an item_id appears more than once")
    missing, extra = sorted(set(given) - set(returned)), sorted(set(returned) - set(given))
    if missing:
        problems.append(f"items missing from the output: {missing}")
    if extra:
        problems.append(f"items not in the batch: {extra}")
    for r in output["items"]:
        item = given.get(r["item_id"])
        if item is None:
            continue
        iid = r["item_id"]
        if RANK[r["disposition"]] < RANK[item["policy_disposition"]]:
            problems.append(f"{iid}: disposition {r['disposition']} is lower than the policy disposition "
                            f"{item['policy_disposition']}; reviewers may only raise")
        if r["disposition"] != item["policy_disposition"] and not (r.get("raised_reason") or "").strip():
            problems.append(f"{iid}: disposition raised without a raised_reason")
        clauses = parse_clause_refs(item["contract_clause"] or "")
        if clauses and not any(f"§{n}" in r["justification"] for n in clauses):
            problems.append(f"{iid}: justification cites none of the governing clauses "
                            f"{', '.join(f'§{n}' for n in clauses)}")
        problems += check_memo(item, r["memo_markdown"])
    return problems


# ----------------------------------------------------------------------------- accepted lines
def accepted_justification(rec: dict) -> str:
    """Code-written justification for lines no reviewer sees."""
    clause = rec["contract_clause"] or "the contract"
    codes = [f["code"] for f in rec["findings"] if not f["resolved_by"]]
    if "CREDIT_APPLIED" in codes:
        f = next(f for f in rec["findings"] if f["code"] == "CREDIT_APPLIED")
        return f"Valid credit note that {f['detail']}; accepted at the amount credited ({clause})."
    if rec["adjustments"]:
        credited = ", ".join(", ".join(a["by"]) for a in rec["adjustments"])
        return (f"Overbill against {clause} is fully corrected by {credited}; nothing further is due or "
                f"disputed on this line.")
    if "UNDERBILLED" in codes:
        return (f"Billed below the contract amount ({clause}); accepted as billed, BlueFin pays less than the "
                f"contract allows.")
    return f"Billed amount equals the contract amount computed from the shipment record ({clause})."
