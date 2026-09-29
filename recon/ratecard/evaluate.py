"""The rate-card verdict for one contract (decision D1).

Order: each extraction must pass ``check_card``; neither may report an
unsupported clause; the two must agree; the agreed reading must match the
approved snapshot for this contract's hash.

- ``ok``              price with the *snapshot* card, so clause citations and
                      wording are identical on every run
- ``needs_approval``  no snapshot for this contract version; a human reviews
                      the agreed card and approves it
- ``hold``            anything else; the run stops and a human sees why

Nothing here ever picks one reading over another.
"""
from __future__ import annotations

from recon.ratecard import check_card, compare
from recon.ratecard.contract import Contract
from recon.ratecard.snapshot import SnapshotStore


def evaluate_contract(card_a: dict, card_b: dict, contract: Contract, carrier: str,
                      store: SnapshotStore) -> dict:
    result = {"contract": contract.file_name, "carrier": carrier, "contract_sha256": contract.sha256,
              "verdict": "hold", "reasons": [], "differences": [], "card": None, "candidate": None}

    for name, card in (("A", card_a), ("B", card_b)):
        problems = check_card(card, contract, carrier)
        result["reasons"] += [f"extraction {name}: {p}" for p in problems]
    if result["reasons"]:
        return result

    for name, card in (("A", card_a), ("B", card_b)):
        for item in card["unsupported"]:
            result["reasons"].append(f"extraction {name}: unsupported clause {item['clause']}: {item['why']}")
    if result["reasons"]:
        return result

    differences = compare.diff(card_a, card_b)
    if differences:
        result["reasons"].append("extractions A and B disagree")
        result["differences"] = differences
        return result

    status, snapshot_card, snap_diff = store.compare(card_a)
    if status == "missing":
        result["verdict"] = "needs_approval"
        result["reasons"].append(f"no approved snapshot for {contract.file_name} at {contract.sha256[:12]}…")
        result["candidate"] = card_a
        return result
    if status == "differs":
        result["reasons"].append("agreed reading differs from the approved snapshot")
        result["differences"] = snap_diff
        return result

    result["verdict"] = "ok"
    result["card"] = snapshot_card
    return result
