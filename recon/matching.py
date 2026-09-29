"""Shipment matching (decision D5).

A line matches a shipment only on the same carrier and the same reference,
compared after trimming whitespace and ignoring case. Nothing looser ever
sets a shipment id.

For a line that does not match, near-miss *candidates* are reported for a
human to follow up: same carrier, same ship date, weight and distance, and
a reference one edit away. Each candidate carries its delivery status and
whether it is already billed elsewhere. Candidates go into notes and memos
only.
"""
from __future__ import annotations

from collections import defaultdict

from recon.money import D


def norm_ref(ref: str) -> str:
    return ref.strip().upper()


def _one_edit_apart(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return True
    return False


class ShipmentIndex:
    def __init__(self, shipments: list[dict]):
        self.by_ref: dict[tuple[str, str], dict] = {}
        self.by_ref_any_carrier: dict[str, list[dict]] = defaultdict(list)
        self.by_profile: dict[tuple, list[dict]] = defaultdict(list)
        for s in shipments:
            key = (s["carrier"], norm_ref(s["carrier_consignment_ref"]))
            if key in self.by_ref:
                raise ValueError(f"shipment reference {key} appears more than once")
            self.by_ref[key] = s
            self.by_ref_any_carrier[norm_ref(s["carrier_consignment_ref"])].append(s)
            self.by_profile[self._profile(s["carrier"], s["ship_date"], s["billed_weight_kg"],
                                          s["distance_km"])].append(s)

    @staticmethod
    def _profile(carrier: str, date: str | None, kg, km) -> tuple:
        return (carrier, date, D(kg) if kg is not None else None, D(km) if km is not None else None)

    def match(self, carrier: str, ref: str) -> dict | None:
        return self.by_ref.get((carrier, norm_ref(ref)))

    def other_carrier_owner(self, carrier: str, ref: str) -> dict | None:
        others = [s for s in self.by_ref_any_carrier.get(norm_ref(ref), []) if s["carrier"] != carrier]
        return others[0] if others else None

    def candidates(self, line: dict, billed_refs: dict[str, list[str]]) -> list[dict]:
        """Near misses for an unmatched line, using what the invoice printed."""
        printed = line.get("printed") or {}
        date = printed.get("booking_date")
        kg, km = printed.get("weight_kg"), printed.get("distance_km")
        ref = norm_ref(line["consignment_ref"])
        pool: list[dict]
        if date is not None and kg is not None and km is not None:
            pool = self.by_profile.get(self._profile(line["carrier"], date, kg, km), [])
        else:
            # Formats that do not print every attribute: fall back to reference similarity only.
            pool = [s for (c, _), s in self.by_ref.items() if c == line["carrier"]]
        found = []
        for s in pool:
            sref = norm_ref(s["carrier_consignment_ref"])
            if _one_edit_apart(ref, sref):
                found.append({
                    "shipment_id": s["shipment_id"], "consignment_ref": s["carrier_consignment_ref"],
                    "ship_date": s["ship_date"], "billed_weight_kg": s["billed_weight_kg"],
                    "distance_km": s["distance_km"], "delivery_status": s["delivery_status"],
                    "billed_elsewhere": sorted(billed_refs.get(sref, [])),
                    "profile_match": date is not None,
                })
        return sorted(found, key=lambda c: c["consignment_ref"])

