"""The pricing engine: what the contract says a shipment should cost.

Inputs are the shipment record (ground truth: distance, billed weight,
service level, special handling) and a rate card. Nothing printed on the
invoice is used. Arithmetic is exact; the line total is rounded half-up to
the paisa once, at the end.

When no band prices the shipment the result is *undetermined*:

- ``CONTRACT_GAP``  the weight falls between two defined bands (Alpine's
                    exactly-50 kg); candidates under each adjacent band are
                    returned so a human can see what is at stake
- ``OUT_OF_CARD``   the weight is beyond every band (Falcon above 2,000 kg);
                    no candidates, because the contract says it is quoted
                    case by case
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from recon.money import D, ZERO, q2


def condition_met(when: dict, shipment: dict) -> bool:
    if when.get("always"):
        return True
    if "service_level" in when:
        return shipment.get("service_level") == when["service_level"]
    if "special_handling" in when:
        return when["special_handling"] in (shipment.get("special_handling") or [])
    raise ValueError(f"unknown condition {when}")


def _in_band(band: dict, weight: Decimal) -> bool:
    lo, hi = band["min_kg"], band["max_kg"]
    if lo is not None:
        lo = Decimal(lo)
        if weight < lo or (weight == lo and not band["min_inclusive"]):
            return False
    if hi is not None:
        hi = Decimal(hi)
        if weight > hi or (weight == hi and not band["max_inclusive"]):
            return False
    return True


def _adjacent_bands(bands: list[dict], weight: Decimal) -> tuple[dict | None, dict | None]:
    """The nearest band below and above a weight that no band covers."""
    below = [b for b in bands if b["max_kg"] is not None and Decimal(b["max_kg"]) <= weight]
    above = [b for b in bands if b["min_kg"] is not None and Decimal(b["min_kg"]) >= weight]
    lower = max(below, key=lambda b: Decimal(b["max_kg"])) if below else None
    upper = min(above, key=lambda b: Decimal(b["min_kg"])) if above else None
    return lower, upper


def weights(shipment: dict, card: dict) -> dict[str, Decimal]:
    billed = D(shipment["billed_weight_kg"])
    chargeable = billed
    if card["chargeable_weight"] is not None:
        chargeable = max(billed, D(card["chargeable_weight"]["minimum_kg"]))
    return {"billed": billed, "chargeable": chargeable}


def _describe_band(band: dict) -> str:
    lo = "" if band["min_kg"] is None else f"{'≥' if band['min_inclusive'] else '>'}{band['min_kg']} kg"
    hi = "" if band["max_kg"] is None else f"{'≤' if band['max_inclusive'] else '<'}{band['max_kg']} kg"
    return " and ".join(p for p in (lo, hi) if p) or "all weights"


def price(shipment: dict, card: dict, *, force_bands: dict[int, dict] | None = None) -> dict[str, Any]:
    """Price one shipment. ``force_bands`` (component index → band) is used for candidates."""
    w = weights(shipment, card)
    distance = D(shipment["distance_km"])
    clauses: list[str] = []
    components = []
    for index, comp in enumerate(card["freight"]["components"]):
        weight = w[comp["weight_basis"]]
        band = (force_bands or {}).get(index)
        if band is None:
            matches = [b for b in comp["bands"] if _in_band(b, weight)]
            if not matches:
                return _undetermined(shipment, card, index, comp, weight)
            band = matches[0]
        quantity = distance if comp["basis"] == "per_km" else weight
        amount = D(band["rate"]) * quantity
        components.append({"label": comp.get("label") or comp["basis"], "basis": comp["basis"],
                           "weight_basis": comp["weight_basis"], "weight_kg": weight,
                           "distance_km": distance if comp["basis"] == "per_km" else None,
                           "band": _describe_band(band), "rate": D(band["rate"]),
                           "quantity": quantity, "amount": amount, "clause": comp["clause"]})
        clauses.append(comp["clause"])
    if card["chargeable_weight"] is not None and any(c["weight_basis"] == "chargeable" for c in components):
        clauses.append(card["chargeable_weight"]["clause"])
    freight = sum((c["amount"] for c in components), ZERO)

    surcharges = []
    applied_total = ZERO
    for s in card["surcharges"]:
        if not condition_met(s["when"], shipment):
            continue
        base = freight if s["base"] == "freight" else freight + applied_total
        amount = base * D(s["pct"]) / Decimal(100)
        surcharges.append({"label": s.get("label") or "surcharge", "pct": D(s["pct"]), "base": base,
                           "amount": amount, "clause": s["clause"]})
        applied_total += amount
        clauses.append(s["clause"])

    accessorials = []
    for a in card["accessorials"]:
        if condition_met(a["when"], shipment):
            accessorials.append({"label": a.get("label") or "accessorial", "amount": D(a["amount"]),
                                 "clause": a["clause"], "when": a["when"]})
            clauses.append(a["clause"])

    exact = freight + applied_total + sum((a["amount"] for a in accessorials), ZERO)
    flags = []
    if shipment.get("service_level") not in card["service_levels"]["allowed"]:
        flags.append("service_not_offered")
    return {"status": "priced", "expected": q2(exact), "exact": exact, "freight": freight,
            "components": components, "surcharges": surcharges, "accessorials": accessorials,
            "weights": w, "clauses": _unique(clauses), "flags": flags,
            "code": None, "reason": None, "candidates": []}


def _undetermined(shipment: dict, card: dict, index: int, comp: dict, weight: Decimal) -> dict[str, Any]:
    lower, upper = _adjacent_bands(comp["bands"], weight)
    gap = lower is not None and upper is not None
    candidates = []
    if gap:
        for band in (lower, upper):
            result = price(shipment, card, force_bands={index: band})
            if result["status"] == "priced":
                candidates.append({"band": _describe_band(band), "rate": D(band["rate"]),
                                   "amount": result["expected"]})
    code = "CONTRACT_GAP" if gap else "OUT_OF_CARD"
    reason = (f"{weight} kg ({comp['weight_basis']} weight) falls between the bands the contract defines "
              f"({_describe_band(lower)}; {_describe_band(upper)})" if gap else
              f"{weight} kg ({comp['weight_basis']} weight) is outside every band the contract defines")
    return {"status": "undetermined", "expected": None, "exact": None, "freight": None,
            "components": [], "surcharges": [], "accessorials": [], "weights": weights(shipment, card),
            "clauses": [comp["clause"]], "flags": [], "code": code, "reason": reason,
            "candidates": candidates}


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        for part in (p.strip() for p in item.split(",")):
            if part not in seen:
                seen.append(part)
    return seen
