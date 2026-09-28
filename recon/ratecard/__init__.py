"""Rate cards: a contract expressed with a closed set of pricing building blocks.

``check_card`` is the per-card gate (schema, identity, clause citations,
verbatim quotes, numbers-in-quote, band sanity, clause coverage). Comparison
between cards and against the approved snapshot lives in ``compare`` and
``snapshot``; ``evaluate`` combines them into the run's verdict.
"""
from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator

from recon.ratecard.contract import Contract, contains_quote, numbers_in, parse_clause_refs

SCHEMA_PATH = Path(__file__).with_name("schema.json")


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def schema_problems(card: Any) -> list[str]:
    errors = sorted(_validator().iter_errors(card), key=lambda e: list(e.absolute_path))
    return [f"schema: /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors]


def iter_cited_rules(card: dict) -> Iterator[tuple[str, dict, list[Decimal]]]:
    """Yield (json path, rule, numbers that must appear in the rule's quote)."""
    for i, comp in enumerate(card["freight"]["components"]):
        nums = []
        for band in comp["bands"]:
            nums += [Decimal(band[k]) for k in ("min_kg", "max_kg") if band[k] is not None]
            nums.append(Decimal(band["rate"]))
        yield f"/freight/components/{i}", comp, nums
    if card["chargeable_weight"] is not None:
        yield "/chargeable_weight", card["chargeable_weight"], [Decimal(card["chargeable_weight"]["minimum_kg"])]
    yield "/service_levels", card["service_levels"], []
    for i, s in enumerate(card["surcharges"]):
        yield f"/surcharges/{i}", s, [Decimal(s["pct"])]
    for i, a in enumerate(card["accessorials"]):
        yield f"/accessorials/{i}", a, [Decimal(a["amount"])]
    yield "/other_accessorials", card["other_accessorials"], []
    for i, d in enumerate(card["invoice_discounts"]):
        yield f"/invoice_discounts/{i}", d, [Decimal(d["threshold_gt"]), Decimal(d["pct"])]
    for key in ("credit_notes", "unmatched_lines"):
        if card[key] is not None:
            yield f"/{key}", card[key], []
    if card["dispute_window"] is not None:
        yield "/dispute_window", card["dispute_window"], [Decimal(card["dispute_window"]["days"])]
    for i, u in enumerate(card["unsupported"]):
        yield f"/unsupported/{i}", u, []


def _band_problems(path: str, bands: list[dict]) -> list[str]:
    """Bands must not overlap: a weight priced by two bands is ambiguous."""
    def covers(band: dict, w: Decimal) -> bool:
        lo, hi = band["min_kg"], band["max_kg"]
        if lo is not None and (w < Decimal(lo) or (w == Decimal(lo) and not band["min_inclusive"])):
            return False
        if hi is not None and (w > Decimal(hi) or (w == Decimal(hi) and not band["max_inclusive"])):
            return False
        return True

    problems = []
    for b in bands:
        if b["min_kg"] is not None and b["max_kg"] is not None and Decimal(b["min_kg"]) > Decimal(b["max_kg"]):
            problems.append(f"{path}: band min {b['min_kg']} exceeds max {b['max_kg']}")
    probes = sorted({Decimal(b[k]) for b in bands for k in ("min_kg", "max_kg") if b[k] is not None})
    probes = probes + [p + Decimal("0.001") for p in probes] + [p - Decimal("0.001") for p in probes]
    for w in sorted(set(probes)):
        hits = [b for b in bands if covers(b, w)]
        if len(hits) > 1:
            problems.append(f"{path}: weight {w} kg falls in more than one band")
            break
    return problems


def check_card(card: Any, contract: Contract, carrier: str) -> list[str]:
    """Return every reason this card cannot be trusted (empty list = passes)."""
    problems = schema_problems(card)
    if problems:
        return problems
    if card["contract_sha256"] != contract.sha256:
        problems.append(f"contract_sha256 {card['contract_sha256'][:12]}… does not match "
                        f"{contract.file_name} ({contract.sha256[:12]}…)")
    if card["contract_file"] != contract.file_name:
        problems.append(f"contract_file {card['contract_file']!r} is not {contract.file_name!r}")
    if card["carrier"] != carrier:
        problems.append(f"carrier {card['carrier']!r} is not the assigned carrier {carrier!r}")

    cited: set[int] = set()
    for path, rule, numbers in iter_cited_rules(card):
        refs = parse_clause_refs(rule["clause"])
        missing = [n for n in refs if n not in contract.clauses]
        if missing:
            problems.append(f"{path}: cites clause(s) {missing} that {contract.file_name} does not have")
            continue
        cited.update(refs)
        clause_text = contract.clause_text(refs)
        if not contains_quote(clause_text, rule["quote"]):
            problems.append(f"{path}: quote not found verbatim in {rule['clause']}: {rule['quote']!r}")
            continue
        in_quote = numbers_in(rule["quote"])
        for n in numbers:
            if n not in in_quote:
                problems.append(f"{path}: value {n} does not appear in its quote {rule['quote']!r}")
    for i, comp in enumerate(card["freight"]["components"]):
        problems += _band_problems(f"/freight/components/{i}", comp["bands"])
    for item in card["non_pricing"]:
        refs = parse_clause_refs(item["clause"])
        missing = [n for n in refs if n not in contract.clauses]
        if missing:
            problems.append(f"/non_pricing: cites clause(s) {missing} that {contract.file_name} does not have")
        cited.update(refs)
    uncovered = sorted(set(contract.clauses) - cited)
    if uncovered:
        problems.append("clauses not accounted for by any rule or non_pricing entry: "
                        + ", ".join(f"§{n}" for n in uncovered))
    return problems
