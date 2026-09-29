"""Assemble reconciliation-report.json and prove it is right before publishing.

Every total is computed here from the reconciled lines; reviewer output
contributes only dispositions it was allowed to raise, justifications and
memos. ``validate_report`` re-derives every invariant independently of
``assemble`` so an assembly bug cannot validate itself.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from recon.money import D, ZERO, q2, to_number
from recon.review import accepted_justification, check_memo

REPORT_SCHEMA = Path(__file__).resolve().parents[1] / "report.schema.json"
DISPOSITIONS = ("accept", "dispute", "escalate")


def _money(v: Decimal) -> str:
    return f"₹{q2(v):,.2f}"


def _line_notes(rec: dict, review: dict | None) -> str:
    parts = [f"{f['code']}: {f['detail']}" for f in rec["findings"] if not f["resolved_by"]]
    parts += [f"{f['code']} (resolved, {f['resolved_by']}): {f['detail']}" for f in rec["findings"] if f["resolved_by"]]
    parts += rec["notes"]
    if review and review.get("raised_reason"):
        parts.append(f"Reviewer raised the disposition to {review['disposition']}: {review['raised_reason']}")
    return " | ".join(parts)


def _invoice_total(doc: dict, recs: list[dict], summary: dict, findings: list[dict]) -> dict[str, Any]:
    billed_total = D(doc["printed_total"])
    undetermined = [r for r in recs if r["expected"] is None]
    notes = list(summary["notes"])
    expected_total: Decimal | None
    if undetermined or summary["expected_discount"] is None:
        expected_total = None
        known = sum((r["expected"] for r in recs if r["expected"] is not None), ZERO)
        low = high = ZERO
        for r in undetermined:
            cands = [D(c["amount"]) for c in ((r.get("pricing") or {}).get("candidates") or [])]
            low += min(cands) if cands else ZERO
            high += max(cands) if cands else r["billed_total"]
        rng = summary["discount_range"]
        if rng:
            known_after = known - D(rng["known_lines"])
            low_total, high_total = known + low - D(rng["low"]), known + high - D(rng["high"])
        else:
            known_after = known - D(summary["expected_discount"] or 0)
            low_total, high_total = known_after + low, known_after + high
        notes.append(f"Expected total not determinable: {', '.join(r['line_key'] for r in undetermined)} "
                     f"{'has' if len(undetermined) == 1 else 'have'} no contract amount yet. Determinable lines "
                     f"come to {_money(known_after)}; the total will be {_money(low_total)} to {_money(high_total)} "
                     f"once they are settled.")
    else:
        expected_total = sum((r["expected"] for r in recs), ZERO) - D(summary["expected_discount"])
    held_lines = [r for r in recs if r["disposition"] != "accept"]
    held = sum((r["billed_total"] for r in held_lines), ZERO)
    shortfall = sum((D(f["amount_impact"]) for f in findings
                     if f["disposition"] == "dispute" and f["amount_impact"] is not None), ZERO)
    payable = sum((r["billed_total"] for r in recs if r["disposition"] == "accept"), ZERO) \
        - D(summary["billed_discount"]) - shortfall
    if held_lines or shortfall:
        notes.append(f"Net payable now {_money(payable)} (accepted lines less billed discount"
                     + (f" and the {_money(shortfall)} disputed at invoice level" if shortfall else "")
                     + f"); held {_money(held)} across {len(held_lines)} disputed or escalated line(s).")
    out = {"invoice": doc["doc_id"], "billed_total": to_number(billed_total),
           "expected_total": to_number(expected_total)}
    if notes:
        out["notes"] = " ".join(notes)
    return out


def assemble(reconciled: dict, documents: list[dict], reviews: dict[str, dict]) -> dict[str, Any]:
    """Build the report. ``reviews`` maps item_id → reviewer record for every non-accept item."""
    missing = [r["line_key"] for r in reconciled["lines"] if r["disposition"] != "accept" and r["line_key"] not in reviews]
    missing += [f["finding_id"] for f in reconciled["invoice_findings"]
                if f["disposition"] != "accept" and f["finding_id"] not in reviews]
    if missing:
        raise ValueError(f"no review for non-accept items: {missing}")

    lines_out = []
    for rec in reconciled["lines"]:
        review = reviews.get(rec["line_key"])
        if review:
            rec = {**rec, "disposition": review["disposition"]}
        entry = {
            "invoice": rec["doc_id"], "consignment_ref": rec["consignment_ref"], "shipment_id": rec["shipment_id"],
            "billed_amount": to_number(rec["billed_total"]), "expected_amount": to_number(rec["expected"]),
            "delta": to_number(rec["delta"]), "disposition": rec["disposition"],
            "justification": review["justification"] if review else accepted_justification(rec),
            "contract_clause": rec["contract_clause"],
        }
        notes = _line_notes(rec, review)
        if notes:
            entry["notes"] = notes
        lines_out.append((rec, entry))

    findings_out = []
    final_findings = []
    for f in reconciled["invoice_findings"]:
        review = reviews.get(f["finding_id"])
        disposition = review["disposition"] if review else f["disposition"]
        final_findings.append({**f, "disposition": disposition})
        findings_out.append({
            "invoice": f["doc_id"],
            "description": f["detail"].rstrip(".") + "." + ("".join(" " + n for n in f["notes"]) if f["notes"] else ""),
            "amount_impact": to_number(f["amount_impact"]), "disposition": disposition,
            "justification": review["justification"] if review else f["detail"],
            "contract_clause": f["contract_clause"],
        })

    recs_final = [rec for rec, _ in lines_out]
    totals = []
    for doc in documents:
        recs = [r for r in recs_final if r["doc_id"] == doc["doc_id"]]
        doc_findings = [f for f in final_findings if f["doc_id"] == doc["doc_id"]]
        totals.append(_invoice_total(doc, recs, reconciled["documents"][doc["doc_id"]], doc_findings))

    report_lines = [entry for _, entry in lines_out]
    counts = {d: sum(1 for l in report_lines if l["disposition"] == d) for d in DISPOSITIONS}
    expected_totals = [t["expected_total"] for t in totals]
    in_dispute = sum((abs(D(l["delta"])) for l in report_lines if l["disposition"] == "dispute" and l["delta"] is not None), ZERO) \
        + sum((abs(D(f["amount_impact"])) for f in findings_out
               if f["disposition"] == "dispute" and f["amount_impact"] is not None), ZERO)
    summary = {
        "total_billed": to_number(sum((D(t["billed_total"]) for t in totals), ZERO)),
        "total_expected": None if any(e is None for e in expected_totals)
        else to_number(sum((D(e) for e in expected_totals), ZERO)),
        "total_in_dispute": to_number(in_dispute),
        "line_count": len(report_lines),
        "counts_by_disposition": counts,
    }
    return {"lines": report_lines, "invoice_findings": findings_out, "invoice_totals": totals, "summary": summary}


def memo_files(reconciled: dict, reviews: dict[str, dict], items: dict[str, dict]) -> dict[str, str]:
    """File name → memo markdown for every non-accept item after review."""
    out = {}
    for item_id, review in sorted(reviews.items()):
        if review["disposition"] == "accept":
            continue
        out[items[item_id]["memo_file"]] = review["memo_markdown"].rstrip() + "\n"
    return out


# ----------------------------------------------------------------------------- validation
def validate_report(report: dict, *, parsed_lines: list[dict], documents: list[dict], reconciled: dict,
                    items: dict[str, dict], memos: dict[str, str]) -> list[str]:
    schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
    problems = [f"schema: /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                for e in sorted(Draft7Validator(schema).iter_errors(report), key=lambda e: list(e.absolute_path))]
    if problems:
        return problems

    # Every parsed line exactly once, in order.
    lines = report["lines"]
    if len(lines) != len(parsed_lines):
        problems.append(f"report has {len(lines)} lines, {len(parsed_lines)} were parsed")
    for i, (out, src) in enumerate(zip(lines, parsed_lines)):
        if (out["invoice"], out["consignment_ref"]) != (src["doc_id"], src["consignment_ref"]) \
                or D(out["billed_amount"]) != D(src["billed_total"]):
            problems.append(f"line {i}: report has {out['invoice']}/{out['consignment_ref']} "
                            f"{out['billed_amount']}, parsed {src['line_key']} {src['billed_total']}")

    # Amount invariants.
    for i, l in enumerate(lines):
        where = f"{l['invoice']}/{l['consignment_ref']} (line {i})"
        if l["expected_amount"] is None:
            if l["delta"] is not None:
                problems.append(f"{where}: delta must be null when expected is null")
            if l["disposition"] == "accept":
                problems.append(f"{where}: accepted with no expected amount")
        elif l["delta"] is None or D(l["delta"]) != q2(D(l["billed_amount"]) - D(l["expected_amount"])):
            problems.append(f"{where}: delta {l['delta']} is not billed − expected")
        if l["disposition"] == "accept" and l["delta"] is not None and D(l["delta"]) > 0:
            problems.append(f"{where}: accepted although billed exceeds expected")

    # Invoice totals.
    by_doc: dict[str, list[dict]] = {}
    for l in lines:
        by_doc.setdefault(l["invoice"], []).append(l)
    totals = {t["invoice"]: t for t in report["invoice_totals"]}
    if sorted(totals) != sorted(d["doc_id"] for d in documents) or len(totals) != len(report["invoice_totals"]):
        problems.append("invoice_totals must list every document exactly once")
    for doc in documents:
        t = totals.get(doc["doc_id"])
        if t is None:
            continue
        doc_lines = by_doc.get(doc["doc_id"], [])
        discount = D(doc["printed_discount"] or 0)
        billed_sum = sum((D(l["billed_amount"]) for l in doc_lines), ZERO)
        if D(t["billed_total"]) != D(doc["printed_total"]) or D(t["billed_total"]) != billed_sum - discount:
            problems.append(f"{doc['doc_id']}: billed_total {t['billed_total']} does not equal lines less discount "
                            f"({billed_sum - discount}) and the printed total ({doc['printed_total']})")
        summary = reconciled["documents"][doc["doc_id"]]
        undeterminable = any(l["expected_amount"] is None for l in doc_lines) or summary["expected_discount"] is None
        if undeterminable != (t["expected_total"] is None):
            problems.append(f"{doc['doc_id']}: expected_total must be null exactly when a component is undeterminable")
        elif not undeterminable:
            want = sum((D(l["expected_amount"]) for l in doc_lines), ZERO) - D(summary["expected_discount"])
            if D(t["expected_total"]) != q2(want):
                problems.append(f"{doc['doc_id']}: expected_total {t['expected_total']} should be {q2(want)}")

    # Invoice findings are invoice-level only, so their amounts are never also carried by a line.
    if len(report["invoice_findings"]) != len(reconciled["invoice_findings"]):
        problems.append("invoice_findings do not match the reconciliation")

    # Summary.
    s = report["summary"]
    if s["line_count"] != len(lines):
        problems.append(f"summary.line_count {s['line_count']} but {len(lines)} lines")
    for d in DISPOSITIONS:
        n = sum(1 for l in lines if l["disposition"] == d)
        if s["counts_by_disposition"][d] != n:
            problems.append(f"summary.counts_by_disposition.{d} is {s['counts_by_disposition'][d]}, lines say {n}")
    if D(s["total_billed"]) != sum((D(t["billed_total"]) for t in report["invoice_totals"]), ZERO):
        problems.append("summary.total_billed is not the sum of invoice billed totals")
    if any(t["expected_total"] is None for t in report["invoice_totals"]):
        if s["total_expected"] is not None:
            problems.append("summary.total_expected must be null when any invoice expected_total is null")
    elif s["total_expected"] is None or D(s["total_expected"]) != sum((D(t["expected_total"]) for t in report["invoice_totals"]), ZERO):
        problems.append("summary.total_expected is not the sum of invoice expected totals")
    in_dispute = sum((abs(D(l["delta"])) for l in lines if l["disposition"] == "dispute" and l["delta"] is not None), ZERO) \
        + sum((abs(D(f["amount_impact"])) for f in report["invoice_findings"]
               if f["disposition"] == "dispute" and f["amount_impact"] is not None), ZERO)
    if D(s["total_in_dispute"]) != q2(in_dispute):
        problems.append(f"summary.total_in_dispute {s['total_in_dispute']} should be {q2(in_dispute)}")

    # Memos: exactly one per non-accept item, with the right figures.
    non_accept = {}
    for rec, l in zip(reconciled["lines"], lines):
        if l["disposition"] != "accept":
            non_accept[rec["line_key"]] = l
    for f, out in zip(reconciled["invoice_findings"], report["invoice_findings"]):
        if out["disposition"] != "accept":
            non_accept[f["finding_id"]] = out
    expected_files = {}
    for item_id in non_accept:
        item = items.get(item_id)
        if item is None:
            problems.append(f"{item_id}: non-accept item was never reviewed")
            continue
        expected_files[item["memo_file"]] = item
    if set(memos) != set(expected_files):
        missing = sorted(set(expected_files) - set(memos))
        extra = sorted(set(memos) - set(expected_files))
        if missing:
            problems.append(f"memos missing: {missing}")
        if extra:
            problems.append(f"memos without a non-accept item: {extra}")
    for name, item in sorted(expected_files.items()):
        if name in memos:
            problems += check_memo(item, memos[name])
    return problems
