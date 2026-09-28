"""Flow-stage logic around the agent steps: planning compile jobs, the in-child
card comparison, the parent rates check, and settling a review child.

Each function is deterministic and returns plain data; ``recon.__main__``
wraps them as commands that flow scripts call.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from recon.model import read_json, write_json
from recon.ratecard import check_card, compare, strip_metadata
from recon.ratecard.contract import load_contract
from recon.ratecard.evaluate import evaluate_contract
from recon.ratecard.snapshot import SnapshotStore
from recon.review import check_review

RATE_CARD_SCHEMA = Path(__file__).with_name("ratecard") / "schema.json"


def vocabulary(shipments: list[dict]) -> dict[str, Any]:
    """The condition values a rate card may use, taken from the shipment records."""
    return {
        "service_level": sorted({s["service_level"] for s in shipments}),
        "special_handling": sorted({h for s in shipments for h in s.get("special_handling") or []}),
        "note": "Rate-card conditions must use exactly these values; they are the values shipment records carry.",
    }


def plan_compile(carriers: list[dict], repo_root: Path, vocabulary_path: Path) -> list[dict]:
    """One compile job per carrier contract (the dynamic_fanout source list)."""
    jobs = []
    for entry in sorted(carriers, key=lambda c: c["carrier"]):
        contract = load_contract(repo_root / entry["contract"])
        jobs.append({"carrier": entry["carrier"], "carrier_name": entry["name"],
                     "contract_path": str((repo_root / entry["contract"]).resolve()),
                     "contract_file": contract.file_name, "contract_sha256": contract.sha256,
                     "card_schema_path": str(RATE_CARD_SCHEMA.resolve()),
                     "vocabulary_path": str(Path(vocabulary_path).resolve()),
                     "repo_root": str(Path(repo_root).resolve())})
    return jobs


def compare_cards(card_a_path: Path, card_b_path: Path, contract_path: Path, carrier: str,
                  vocab: dict[str, Any] | None = None) -> dict[str, Any]:
    """The in-child agreement gate: both cards pass every check and read the contract the same way."""
    contract = load_contract(contract_path)
    report: dict[str, Any] = {"carrier": carrier, "contract_file": contract.file_name,
                              "card_a": str(card_a_path), "card_b": str(card_b_path),
                              "problems_a": [], "problems_b": [], "differences": []}
    cards = {}
    for key, path in (("a", card_a_path), ("b", card_b_path)):
        try:
            cards[key] = read_json(path)
        except (OSError, ValueError) as exc:
            report[f"problems_{key}"].append(f"cannot read card: {exc}")
            continue
        report[f"problems_{key}"] = check_card(cards[key], contract, carrier)
        if vocab:
            report[f"problems_{key}"] += vocabulary_problems(strip_metadata(cards[key]), vocab)
    if "a" in cards and "b" in cards and not report["problems_a"] and not report["problems_b"]:
        report["differences"] = compare.diff(cards["a"], cards["b"])
    report["agree"] = not (report["problems_a"] or report["problems_b"] or report["differences"]
                           or len(cards) < 2)
    return report


def vocabulary_problems(card: dict, vocab: dict[str, Any]) -> list[str]:
    problems = []
    rules = [("/surcharges", s["when"]) for s in card.get("surcharges", [])]
    rules += [("/accessorials", a["when"]) for a in card.get("accessorials", [])]
    for path, when in rules:
        for key in ("service_level", "special_handling"):
            if key in when and when[key] not in vocab[key]:
                problems.append(f"{path}: condition {key}={when[key]!r} is not one of {vocab[key]}")
    for level in card.get("service_levels", {}).get("allowed", []):
        if level not in vocab["service_level"]:
            problems.append(f"/service_levels: {level!r} is not one of {vocab['service_level']}")
    return problems


def rates_check(compile_results: dict[str, str], carriers: list[dict], repo_root: Path, store_root: Path,
                out_dir: Path) -> dict[str, Any]:
    """The parent's D1 verdict across all contracts; writes the cards to price with."""
    store = SnapshotStore(store_root)
    out_dir = Path(out_dir)
    verdicts, approved = [], {}
    results = {read_json(p)["carrier"]: read_json(p) for p in compile_results.values()}
    for entry in sorted(carriers, key=lambda c: c["carrier"]):
        carrier = entry["carrier"]
        contract = load_contract(repo_root / entry["contract"])
        result = results.get(carrier)
        if result is None:
            verdicts.append({"carrier": carrier, "verdict": "hold", "reasons": ["no compile result"],
                             "differences": []})
            continue
        v = evaluate_contract(read_json(result["card_a"]), read_json(result["card_b"]), contract, carrier, store)
        if v["verdict"] == "ok":
            approved[carrier] = v["card"]
        if v["verdict"] == "needs_approval":
            candidate = out_dir / f"candidate-{carrier}.json"
            write_json(candidate, strip_metadata(v["candidate"]))
            v["candidate_path"] = str(candidate.resolve())
            v["approve_command"] = (
                f"orchestrator/.venv/bin/python -m recon approve-rate-card --card {candidate.resolve()} "
                f"--contract {entry['contract']} --carrier {carrier} --approved-by '<your name>' "
                f"--source-run {result.get('run_dir', '<run dir>')}")
        verdicts.append({k: v[k] for k in ("carrier", "verdict", "reasons", "differences",
                                           "contract", "contract_sha256") if k in v}
                        | {k: v[k] for k in ("candidate_path", "approve_command") if k in v})
    order = {"ok": 0, "needs_approval": 1, "hold": 2}
    overall = max((v["verdict"] for v in verdicts), key=lambda x: order[x], default="hold")
    report = {"verdict": overall, "contracts": verdicts}
    write_json(out_dir / "rates-report.json", report)
    if overall == "ok":
        write_json(out_dir / "rate-cards.json", approved)
    return report


def finalize_review(batch_path: Path, attempts: list[Path], out_path: Path) -> dict[str, Any]:
    """Settle a review child: the latest attempt that passes the D7 gate, or a failure record."""
    batch = read_json(batch_path)
    history = []
    for path in reversed([p for p in attempts if Path(p).exists()]):
        output = read_json(path)
        problems = check_review(batch, output)
        history.append({"attempt": str(path), "problems": problems})
        if not problems:
            write_json(out_path, output)
            return {"status": "passed", "batch_id": batch["batch_id"], "attempt": str(path)}
    failure = {"status": "failed", "batch_id": batch["batch_id"], "history": history}
    write_json(out_path, failure)
    return failure


def describe_card(card: dict) -> str:
    """A plain-text reading of a rate card for the human who approves it."""
    def cond(when: dict) -> str:
        if when.get("always"):
            return "always"
        key = next(iter(when))
        return f"when {key} = {when[key]}"

    def band(b: dict) -> str:
        lo = "" if b["min_kg"] is None else f"{'≥' if b['min_inclusive'] else '>'} {b['min_kg']} kg"
        hi = "" if b["max_kg"] is None else f"{'≤' if b['max_inclusive'] else '<'} {b['max_kg']} kg"
        return " and ".join(p for p in (lo, hi) if p) or "any weight"

    out = [f"Rate card for {card['carrier']} ({card['contract_file']}, sha256 {card['contract_sha256'][:12]}…)"]
    if card["chargeable_weight"]:
        out.append(f"- Chargeable weight: the higher of actual and {card['chargeable_weight']['minimum_kg']} kg "
                   f"[{card['chargeable_weight']['clause']}]")
    for c in card["freight"]["components"]:
        unit = "km" if c["basis"] == "per_km" else "kg"
        out.append(f"- Freight {c['basis']} on {c['weight_basis']} weight [{c['clause']}]:")
        out += [f"    {band(b)}: ₹{b['rate']} per {unit}" for b in c["bands"]]
    for s in card["surcharges"]:
        out.append(f"- Surcharge {s['pct']}% of {s['base'].replace('_', ' ')}, {cond(s['when'])} [{s['clause']}]")
    for a in card["accessorials"]:
        out.append(f"- Charge ₹{a['amount']} per consignment, {cond(a['when'])} [{a['clause']}]")
    out.append(f"- Other charges payable: {'yes' if card['other_accessorials']['allowed'] else 'no'} "
               f"[{card['other_accessorials']['clause']}]")
    out.append(f"- Service levels offered: {', '.join(card['service_levels']['allowed'])}")
    for d in card["invoice_discounts"]:
        out.append(f"- {d['pct']}% off the invoice total when more than {d['threshold_gt']} consignments are "
                   f"{d['count_basis']} in a {d['period'].replace('_', ' ')} [{d['clause']}]")
    if card["credit_notes"]:
        cn = card["credit_notes"]
        out.append(f"- Credit notes allowed: {'yes' if cn['allowed'] else 'no'}; must cover the full correction: "
                   f"{'yes' if cn['must_cover_full'] else 'not stated'} [{cn['clause']}]")
    if card["unmatched_lines"]:
        out.append(f"- Unmatched lines payable: {'yes' if card['unmatched_lines']['payable'] else 'no, until reconciled'} "
                   f"[{card['unmatched_lines']['clause']}]")
    if card["dispute_window"]:
        out.append(f"- Disputes within {card['dispute_window']['days']} days of invoice date "
                   f"[{card['dispute_window']['clause']}]")
    for n in card["non_pricing"]:
        out.append(f"- Not about price {n['clause']}: {n['summary']}")
    for u in card["unsupported"]:
        out.append(f"- UNSUPPORTED {u['clause']}: {u['why']}")
    return "\n".join(out)


def merge_fallback(fallback: dict, work_dir: Path, carriers: list[str]) -> list[str]:
    """Check agent-parsed documents like any parser's output and add them to the work files."""
    from recon.ingest import tie_out
    from recon.model import make_document, make_line, read_jsonl, write_jsonl

    work_dir = Path(work_dir)
    problems: list[str] = []
    documents = read_json(work_dir / "documents.json")
    lines = read_jsonl(work_dir / "lines.jsonl")
    known_ids = {d["doc_id"] for d in documents}
    new_docs, new_lines = [], []
    for raw in fallback.get("documents", []):
        try:
            doc = make_document(**{k: v for k, v in raw.items() if not k.startswith("_")})
        except (TypeError, ValueError) as exc:
            problems.append(f"document {raw.get('doc_id')}: {exc}")
            continue
        if doc["carrier"] not in carriers:
            problems.append(f"{doc['doc_id']}: carrier {doc['carrier']!r} is not in the carrier registry")
        if doc["doc_id"] in known_ids:
            problems.append(f"{doc['doc_id']}: document id already exists")
        doc_lines = []
        for raw_line in [l for l in fallback.get("lines", []) if l.get("doc_id") == doc["doc_id"]]:
            try:
                doc_lines.append(make_line(**raw_line))
            except (TypeError, ValueError) as exc:
                problems.append(f"{doc['doc_id']} line {raw_line.get('position')}: {exc}")
        result = tie_out(doc, doc_lines)
        if not result["passed"]:
            problems.append(f"{doc['doc_id']} does not tie out: {'; '.join(result['problems'])}")
        new_docs.append(doc)
        new_lines += doc_lines
    if not problems:
        write_json(work_dir / "documents.json", documents + new_docs)
        write_jsonl(work_dir / "lines.jsonl", lines + new_lines)
    return problems
