"""Deterministic reconciliation of every line against shipments and rate cards.

Order of operations (each step only reads what earlier steps settled):

1. per invoice line: exact match, pricing, line checks
   (arithmetic, accessorials, record mismatches, delivery, billing period),
   and the unexplained remainder as a rate mismatch
2. duplicates across invoices (D4)
3. credit notes applied to their original line (D3)
4. delta and policy disposition per line
5. invoice-level volume discounts (D6)
6. dispute-window notes (D8)

No amount here comes from an agent. ``expected`` is always derived from the
approved rate card and the shipment record, adjusted only by the D3/D4 rules.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from recon import policy
from recon.matching import ShipmentIndex, norm_ref
from recon.money import D, ZERO, q2
from recon.policy import finding
from recon.pricing import price

SHIPMENT_FIELDS = ("shipment_id", "carrier_consignment_ref", "ship_date", "distance_km", "billed_weight_kg",
                   "service_level", "special_handling", "delivery_status", "delivered_at")


class ReconcileError(RuntimeError):
    pass


def _clauses_of(ref: str | None) -> list[str]:
    return [p.strip() for p in ref.split(",")] if ref else []


def _add_clauses(rec: dict, ref: str | list[str] | None) -> None:
    for c in (ref if isinstance(ref, list) else _clauses_of(ref)):
        if c and c not in rec["clauses"]:
            rec["clauses"].append(c)


def _money(v: Decimal) -> str:
    return f"₹{q2(v):,.2f}"


class Reconciler:
    def __init__(self, documents: list[dict], lines: list[dict], shipments: list[dict], cards: dict[str, dict]):
        self.docs = {d["doc_id"]: d for d in documents}
        self.lines = lines
        self.shipments = shipments
        self.cards = cards
        self.index = ShipmentIndex(shipments)
        missing = sorted({l["carrier"] for l in lines} - set(cards))
        if missing:
            raise ReconcileError(f"no approved rate card for carrier(s) {missing}")
        self.recs: dict[str, dict] = {}
        self.invoice_findings: list[dict] = []
        self.billed_refs: dict[str, list[str]] = defaultdict(list)
        for l in lines:
            if l["doc_type"] == "invoice":
                self.billed_refs[norm_ref(l["consignment_ref"])].append(l["line_key"])
        self.line_by_key = {l["line_key"]: l for l in lines}
        self.lines_by_doc: dict[str, list[dict]] = defaultdict(list)
        for l in lines:
            self.lines_by_doc[l["doc_id"]].append(l)
        self.doc_order = {doc_id: self._doc_order_key(doc_id) for doc_id in self.docs}

    # ------------------------------------------------------------------ helpers
    def _doc_order_key(self, doc_id: str) -> tuple:
        doc = self.docs[doc_id]
        start = doc["period_start"]
        if start is None:
            dates = sorted(l["printed"].get("booking_date") for l in self.lines_by_doc.get(doc_id, [])
                           if l["printed"].get("booking_date"))
            start = dates[0] if dates else "9999-12-31"
        return (start, doc_id)

    def _new_rec(self, line: dict) -> dict:
        card = self.cards[line["carrier"]]
        return {
            "line_key": line["line_key"], "doc_id": line["doc_id"], "doc_type": line["doc_type"],
            "carrier": line["carrier"], "position": line["position"],
            "consignment_ref": line["consignment_ref"], "source": line["source"],
            "billed_total": D(line["billed_total"]), "billed_components": line["billed_components"],
            "printed": line["printed"], "reason": line.get("reason"),
            "shipment_id": None, "shipment": None, "pricing": None,
            "contract_amount": None, "expected": None, "delta": None,
            "findings": [], "notes": [], "clauses": [], "contract_file": card["contract_file"],
            "candidates": [], "adjustments": [], "disposition": None,
        }

    def _attach_shipment(self, rec: dict, s: dict) -> None:
        rec["shipment_id"] = s["shipment_id"]
        rec["shipment"] = {k: s.get(k) for k in SHIPMENT_FIELDS}

    # ------------------------------------------------------------------ step 1
    def _price_and_check(self, rec: dict, line: dict) -> None:
        card = self.cards[rec["carrier"]]
        s = self.index.match(rec["carrier"], rec["consignment_ref"])
        if s is None:
            owner = self.index.other_carrier_owner(rec["carrier"], rec["consignment_ref"])
            if owner is not None:
                rec["findings"].append(finding(
                    "CARRIER_MISMATCH", f"{rec['consignment_ref']} is a {owner['carrier']} booking "
                    f"({owner['shipment_id']}), billed by {rec['carrier']}"))
            else:
                rule = card["unmatched_lines"]
                detail = f"{rec['consignment_ref']} matches no {rec['carrier']} booking"
                if rule is not None and not rule["payable"]:
                    detail += "; the contract makes unmatched lines not payable until reconciled"
                rec["findings"].append(finding("UNMATCHED_REF", detail, clause=rule["clause"] if rule else None))
                if rule:
                    _add_clauses(rec, rule["clause"])
            rec["candidates"] = self.index.candidates(line, self.billed_refs)
            for c in rec["candidates"]:
                elsewhere = (f"already billed on {', '.join(c['billed_elsewhere'])}" if c["billed_elsewhere"]
                             else "not billed on any invoice in this run")
                rec["notes"].append(
                    f"Possible intended booking: {c['consignment_ref']} ({c['shipment_id']}), same "
                    f"{'date, weight and distance' if c['profile_match'] else 'carrier'}, reference one character "
                    f"different; delivery status {c['delivery_status']}; {elsewhere}. Not used as a match.")
            return

        self._attach_shipment(rec, s)
        if s["delivery_status"] != "delivered":
            rec["findings"].append(finding("NOT_DELIVERED", f"{s['shipment_id']} is recorded as "
                                                           f"{s['delivery_status']}"))
        self._record_checks(rec, s)

        p = price(s, card)
        rec["pricing"] = p
        if p["status"] != "priced":
            rec["findings"].append(finding(p["code"], p["reason"], clause=", ".join(p["clauses"])))
            _add_clauses(rec, p["clauses"])
            if p["candidates"]:
                options = "; ".join(f"{_money(c['amount'])} at ₹{c['rate']}/unit ({c['band']})"
                                    for c in p["candidates"])
                rec["notes"].append(f"Candidate amounts under the adjacent contract bands: {options}.")
            return
        if "service_not_offered" in p["flags"]:
            rule = card["service_levels"]
            rec["findings"].append(finding("SERVICE_NOT_OFFERED", f"booked as {s['service_level']}; contract "
                                           f"offers {', '.join(rule['allowed'])}", clause=rule["clause"]))
        rec["contract_amount"] = rec["expected"] = p["expected"]
        _add_clauses(rec, p["clauses"])
        self._component_checks(rec, card, p)

    def _record_checks(self, rec: dict, s: dict) -> None:
        printed = rec["printed"]
        pairs = [("distance_km", "distance_km"), ("weight_kg", "billed_weight_kg"),
                 ("service_level", "service_level"), ("booking_date", "ship_date")]
        diffs = []
        for p_key, s_key in pairs:
            if p_key not in printed or printed[p_key] in (None, ""):
                continue
            pv, sv = printed[p_key], s[s_key]
            same = (D(pv) == D(sv)) if p_key in ("distance_km", "weight_kg") else (str(pv) == str(sv))
            if not same:
                diffs.append(f"{p_key} printed {pv}, record {sv}")
        if diffs:
            rec["findings"].append(finding("RECORD_MISMATCH", "; ".join(diffs)))
        doc = self.docs[rec["doc_id"]]
        if doc["period_start"] and doc["period_end"] and not (doc["period_start"] <= s["ship_date"] <= doc["period_end"]):
            rec["findings"].append(finding("OUT_OF_PERIOD", f"shipped {s['ship_date']}, invoice period "
                                                           f"{doc['period_start']} to {doc['period_end']}"))

    def _component_checks(self, rec: dict, card: dict, p: dict) -> None:
        billed = rec["billed_total"]
        comps = rec["billed_components"]
        if comps:
            comp_sum = sum((D(c["amount"]) for c in comps), ZERO)
            if comp_sum != billed:
                rec["findings"].append(finding(
                    "ARITHMETIC_ERROR", f"line total {_money(billed)} but its components sum to {_money(comp_sum)}",
                    amount=billed - comp_sum))

        expected_acc = list(p["accessorials"])
        for c in (c for c in comps if c.get("kind") == "accessorial"):
            amount = D(c["amount"])
            pair = next((e for e in expected_acc if D(e["amount"]) == amount), None)
            if pair is not None:
                expected_acc.remove(pair)
                continue
            rule = next((a for a in card["accessorials"] if D(a["amount"]) == amount), None)
            if rule is not None:
                rec["findings"].append(finding(
                    "ACCESSORIAL_NOT_APPLICABLE", f"{c['label']} {_money(amount)} billed, but the shipment record "
                    f"does not meet the contract condition {rule['when']}", amount=amount, clause=rule["clause"]))
                _add_clauses(rec, rule["clause"])
            elif not card["other_accessorials"]["allowed"]:
                rule = card["other_accessorials"]
                rec["findings"].append(finding(
                    "UNAUTHORISED_ACCESSORIAL", f"{c['label']} {_money(amount)} is not a charge the contract allows",
                    amount=amount, clause=rule["clause"]))
                _add_clauses(rec, rule["clause"])
        for e in expected_acc:
            rec["notes"].append(f"Contract charge {e['label']} {_money(D(e['amount']))} applies but was not "
                                f"billed as a separate item.")

        explained = sum((D(f["amount"]) for f in rec["findings"]
                         if f["amount"] is not None and policy.is_monetary(f["code"])), ZERO)
        remainder = billed - rec["expected"] - explained
        if remainder > 0:
            rec["findings"].append(finding(
                "RATE_MISMATCH", f"billed {_money(billed)} against a contract amount of {_money(rec['expected'])}"
                + (f"; {_money(remainder)} not explained by other findings" if explained else ""),
                amount=remainder, clause=", ".join(p["clauses"])))
        elif remainder < 0:
            rec["findings"].append(finding(
                "UNDERBILLED", f"billed {_money(billed)}, below the contract amount of {_money(rec['expected'])}",
                amount=remainder, clause=", ".join(p["clauses"])))

    # ------------------------------------------------------------------ step 2
    def _in_period(self, rec: dict) -> bool:
        doc = self.docs[rec["doc_id"]]
        ship_date = (rec["shipment"] or {}).get("ship_date")
        return bool(ship_date and doc["period_start"] and doc["period_end"]
                    and doc["period_start"] <= ship_date <= doc["period_end"])

    def _duplicates(self) -> None:
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for rec in self.recs.values():
            if rec["doc_type"] == "invoice":
                groups[(rec["carrier"], norm_ref(rec["consignment_ref"]))].append(rec)
        for key in sorted(groups):
            copies = groups[key]
            if len(copies) < 2:
                continue
            ordered = sorted(copies, key=lambda r: (self.doc_order[r["doc_id"]], r["position"]))
            in_period = [r for r in ordered if self._in_period(r)]
            keep = in_period[0] if in_period else ordered[0]
            why = ("its billing period contains the ship date" if in_period
                   else "it is on the earliest invoice (no copy's period contains the ship date)")
            for other in ordered:
                if other is keep:
                    continue
                keep["notes"].append(f"Also billed on {other['doc_id']} line {other['position']}; this copy is "
                                     f"treated as the valid one because {why}.")
                for f in other["findings"]:
                    if policy.disposition_for(f["code"]) != "accept":
                        f["resolved_by"] = "superseded by duplicate-billing finding"
                same = (other["billed_total"] == keep["billed_total"]
                        and other["billed_components"] == keep["billed_components"]
                        and {k: v for k, v in other["printed"].items() if k != "booking_date"}
                        == {k: v for k, v in keep["printed"].items() if k != "booking_date"})
                if same:
                    first_clause = self.cards[other["carrier"]]["freight"]["components"][0]["clause"]
                    other["expected"] = ZERO
                    other["findings"].append(finding(
                        "DUPLICATE_BILLING", f"{other['consignment_ref']} is already billed on {keep['doc_id']} line "
                        f"{keep['position']} with identical details; one booking is charged once",
                        amount=other["billed_total"], clause=first_clause, duplicate_of=keep["line_key"]))
                    _add_clauses(other, first_clause)
                else:
                    other["expected"] = None
                    other["findings"].append(finding(
                        "DUPLICATE_MISMATCH", f"{other['consignment_ref']} is also billed on {keep['doc_id']} line "
                        f"{keep['position']} with different details; which charge is right needs checking",
                        duplicate_of=keep["line_key"]))

    # ------------------------------------------------------------------ step 3
    def _credits(self) -> None:
        invoice_lines = {(r["doc_id"], norm_ref(r["consignment_ref"])): r
                         for r in sorted(self.recs.values(), key=lambda r: r["position"], reverse=True)
                         if r["doc_type"] == "invoice"}
        by_original: dict[str, list[dict]] = defaultdict(list)
        for rec in self.recs.values():
            if rec["doc_type"] != "credit_note":
                continue
            card = self.cards[rec["carrier"]]
            rule = card["credit_notes"]
            s = self.index.match(rec["carrier"], rec["consignment_ref"])
            if s is not None:
                self._attach_shipment(rec, s)
            if rule is None or not rule["allowed"]:
                rec["findings"].append(finding("CREDIT_NOT_PROVIDED_FOR", f"{card['contract_file']} has no "
                                                                          f"credit-note provision"))
                continue
            _add_clauses(rec, rule["clause"])
            target_doc = (self.line_by_key[rec["line_key"]]["references_invoice"]
                          or self.docs[rec["doc_id"]]["references_invoice"])
            original = invoice_lines.get((target_doc, norm_ref(rec["consignment_ref"])))
            if original is None:
                rec["findings"].append(finding(
                    "CREDIT_UNREFERENCED", f"credit references {rec['consignment_ref']} on {target_doc}, "
                    f"which is not a billed line in this run", clause=rule["clause"]))
                continue
            by_original[original["line_key"]].append(rec)

        for key in sorted(by_original):
            original = self.recs[key]
            credits = by_original[key]
            rule = self.cards[original["carrier"]]["credit_notes"]
            credit_total = -sum((c["billed_total"] for c in credits), ZERO)
            names = ", ".join(f"{c['doc_id']}" for c in credits)
            if original["expected"] is None:
                for c in credits:
                    c["findings"].append(finding("CREDIT_ON_UNDETERMINED", f"credit against {original['line_key']}, "
                                                 f"whose contract amount is undetermined", clause=rule["clause"]))
                continue
            overbill = original["billed_total"] - original["expected"]
            if overbill <= 0:
                for c in credits:
                    c["expected"] = ZERO
                    c["findings"].append(finding(
                        "CREDIT_WITHOUT_OVERBILL", f"{original['doc_id']} line {original['position']} was not "
                        f"overbilled (billed {_money(original['billed_total'])}, contract "
                        f"{_money(original['expected'])})", clause=rule["clause"]))
                continue
            original["expected"] = original["expected"] + credit_total
            original["adjustments"].append({"kind": "credit", "amount": credit_total,
                                            "by": [c["line_key"] for c in credits]})
            for f in original["findings"]:
                if policy.is_monetary(f["code"]) and not f["resolved_by"]:
                    f["resolved_by"] = f"credited on {names}"
            open_amount = overbill - credit_total
            original["notes"].append(
                f"Overbilled {_money(overbill)} against the contract; {names} credits {_money(credit_total)}. "
                f"Expected amount on this line is the contract amount plus the credit issued separately, so the "
                f"line's delta is what remains open after the credit.")
            for c in credits:
                c["expected"] = c["billed_total"]
                c["findings"].append(finding("CREDIT_APPLIED", f"corrects {original['doc_id']} line "
                                             f"{original['position']} ({original['consignment_ref']})",
                                             clause=rule["clause"], corrects=original["line_key"]))
            if open_amount > 0:
                clause = rule["clause"] if rule["must_cover_full"] else None
                original["findings"].append(finding(
                    "PARTIAL_CREDIT", f"overbilled {_money(overbill)}; credited {_money(credit_total)} on {names}; "
                    f"{_money(open_amount)} still open"
                    + ("; the contract requires a credit note to cover the full corrected amount"
                       if rule["must_cover_full"] else ""),
                    amount=open_amount, clause=clause))
                _add_clauses(original, clause)
            elif open_amount < 0:
                credits[-1]["findings"].append(finding(
                    "CREDIT_EXCEEDS", f"credits {_money(credit_total)} against an overbill of {_money(overbill)}",
                    clause=rule["clause"]))

    # ------------------------------------------------------------------ step 4
    def _finalise_lines(self) -> None:
        for rec in self.recs.values():
            rec["delta"] = None if rec["expected"] is None else rec["billed_total"] - rec["expected"]
            rec["disposition"] = policy.combine(rec["findings"])
            if rec["expected"] is not None and rec["disposition"] == "accept" and rec["delta"] > 0:
                raise ReconcileError(f"{rec['line_key']}: positive delta {rec['delta']} with no disputing finding")

    # ------------------------------------------------------------------ step 5
    def _discounts(self, tendered: dict[tuple[str, str], int]) -> dict[str, dict]:
        summaries: dict[str, dict] = {}
        recs_by_doc: dict[str, list[dict]] = defaultdict(list)
        for r in self.recs.values():
            recs_by_doc[r["doc_id"]].append(r)
        for doc_id in sorted(self.docs):
            doc = self.docs[doc_id]
            recs = recs_by_doc[doc_id]
            summary = {"doc_id": doc_id, "doc_type": doc["doc_type"], "carrier": doc["carrier"],
                       "billed_discount": D(doc["printed_discount"]) if doc["printed_discount"] is not None else ZERO,
                       "expected_discount": ZERO, "discount_range": None, "notes": [], "dispute_window": None}
            summaries[doc_id] = summary
            card = self.cards[doc["carrier"]]
            if doc["doc_type"] != "invoice":
                continue
            for rule in card["invoice_discounts"]:
                self._apply_discount(doc, recs, rule, summary, tendered)
        return summaries

    def _apply_discount(self, doc: dict, recs: list[dict], rule: dict, summary: dict,
                        tendered: dict[tuple[str, str], int]) -> None:
        card = self.cards[doc["carrier"]]
        clause_ref = f"{card['contract_file']} {rule['clause']}"
        start, end = doc["period_start"], doc["period_end"]
        billed_discount = summary["billed_discount"]
        full_month = bool(start and end and start.endswith("-01")
                          and int(end[-2:]) == calendar.monthrange(int(start[:4]), int(start[5:7]))[1]
                          and start[:7] == end[:7])
        if not full_month:
            if billed_discount != 0:
                summary["notes"].append(f"Discount of {_money(billed_discount)} billed, but the invoice does not "
                                        f"cover one calendar month, so {clause_ref} cannot be checked.")
            self._invoice_finding(doc, "DISCOUNT_PERIOD_AMBIGUOUS",
                                  f"{clause_ref} gives a monthly volume discount on 'that month's invoice', but "
                                  f"this invoice covers {start} to {end}", None, clause_ref)
            return
        month = start[:7]
        count = (tendered.get((doc["carrier"], month), 0) if rule["count_basis"] == "tendered"
                 else sum(1 for r in recs))
        pct = D(rule["pct"]) / Decimal(100)
        if count <= rule["threshold_gt"]:
            summary["notes"].append(f"{count} consignments {rule['count_basis']} in {month}; the {rule['pct']}% "
                                    f"volume discount ({clause_ref}) needs more than {rule['threshold_gt']}.")
            if billed_discount != 0:
                summary["notes"].append(f"A discount of {_money(billed_discount)} was applied anyway.")
            return

        base = recs
        known = sum((r["base_expected"] for r in base if r["base_expected"] is not None), ZERO)
        low = high = ZERO
        undetermined = []
        for r in base:
            if r["base_expected"] is not None:
                continue
            cands = [c["amount"] for c in (r["pricing"] or {}).get("candidates", [])] if r["pricing"] else []
            undetermined.append(r["line_key"])
            if cands:
                low += min(cands)
                high += max(cands)
            else:
                high += r["billed_total"]
        expected_known = q2(known * pct)
        expected_low, expected_high = q2((known + low) * pct), q2((known + high) * pct)
        summary["expected_discount"] = expected_known if not undetermined else None
        summary["discount_range"] = None if not undetermined else {"low": expected_low, "high": expected_high,
                                                                   "known_lines": expected_known}
        summary["notes"].append(
            f"{count} consignments {rule['count_basis']} in {month} (more than {rule['threshold_gt']}): "
            f"{rule['pct']}% volume discount applies ({clause_ref}). Expected discount "
            + (f"{_money(expected_known)}" if not undetermined else
               f"{_money(expected_known)} on determinable lines; {_money(expected_low)} to {_money(expected_high)} "
               f"once {', '.join(undetermined)} is settled")
            + f"; billed {_money(billed_discount)}.")
        if billed_discount >= expected_low:
            if billed_discount > expected_high:
                summary["notes"].append(f"Billed discount exceeds the contract discount by "
                                        f"{_money(billed_discount - expected_high)} (in BlueFin's favour).")
            return
        shortfall = expected_known - billed_discount
        if shortfall > 0:
            detail = (f"volume discount not applied in full: {count} consignments tendered in {month}; "
                      f"{rule['pct']}% of the expected subtotal {_money(known)} is {_money(expected_known)}, "
                      f"billed discount {_money(billed_discount)}")
            notes = []
            if undetermined:
                notes.append(f"A further {_money(expected_low - expected_known)} to "
                             f"{_money(expected_high - expected_known)} depends on {', '.join(undetermined)}, "
                             f"which is escalated separately.")
            self._invoice_finding(doc, "MISSING_DISCOUNT", detail, shortfall, clause_ref, notes)
        else:
            self._invoice_finding(doc, "DISCOUNT_UNDETERMINED",
                                  f"billed discount {_money(billed_discount)} is below the lowest possible contract "
                                  f"discount {_money(expected_low)}; the shortfall depends on "
                                  f"{', '.join(undetermined)}", None, clause_ref)

    def _invoice_finding(self, doc: dict, code: str, detail: str, amount: Decimal | None, clause_ref: str,
                         notes: list[str] | None = None) -> None:
        self.invoice_findings.append({
            "finding_id": f"{doc['doc_id']}#{code.lower()}", "doc_id": doc["doc_id"], "carrier": doc["carrier"],
            "code": code, "detail": detail, "amount_impact": amount, "contract_clause": clause_ref,
            "disposition": policy.disposition_for(code), "notes": list(notes or []),
        })

    # ------------------------------------------------------------------ step 6
    def _dispute_windows(self, summaries: dict[str, dict]) -> None:
        for doc_id, summary in summaries.items():
            doc = self.docs[doc_id]
            rule = self.cards[doc["carrier"]]["dispute_window"]
            if rule is None:
                continue
            printed = doc["doc_date"]
            invoice_date = printed or doc["period_end"]
            if invoice_date is None:
                continue
            closes = (date.fromisoformat(invoice_date) + timedelta(days=rule["days"])).isoformat()
            clause_ref = f"{self.cards[doc['carrier']]['contract_file']} {rule['clause']}"
            summary["dispute_window"] = {"days": rule["days"], "clause": clause_ref, "invoice_date": invoice_date,
                                         "assumed": printed is None, "closes": closes}
            note = (f"Dispute window ({clause_ref}): {rule['days']} days from invoice date; closes {closes} "
                    + (f"if the invoice is dated {invoice_date} (end of billing period, assumed: the invoice date "
                       f"is not printed)." if printed is None else f"(invoice dated {invoice_date})."))
            for rec in self.recs.values():
                if rec["doc_id"] == doc_id and rec["disposition"] != "accept":
                    rec["notes"].append(note)
            for f in self.invoice_findings:
                if f["doc_id"] == doc_id and f["disposition"] != "accept":
                    f["notes"].append(note)

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        for line in self.lines:
            rec = self._new_rec(line)
            self.recs[rec["line_key"]] = rec
            if line["doc_type"] == "invoice":
                self._price_and_check(rec, line)
        self._duplicates()
        for rec in self.recs.values():
            rec["base_expected"] = rec["expected"]  # before credit adjustments: the discount base
        self._credits()
        self._finalise_lines()
        tendered: dict[tuple[str, str], int] = defaultdict(int)
        for s in self.shipments:
            tendered[(s["carrier"], s["ship_date"][:7])] += 1
        summaries = self._discounts(tendered)
        self._dispute_windows(summaries)
        lines_out = []
        for line in self.lines:
            rec = self.recs[line["line_key"]]
            rec["contract_clause"] = (f"{rec['contract_file']} {', '.join(sorted(rec['clauses'], key=_clause_no))}"
                                      if rec["clauses"] else None)
            lines_out.append(rec)
        return {"lines": lines_out, "invoice_findings": self.invoice_findings, "documents": summaries,
                "cards": {c: {"contract_file": card["contract_file"], "contract_sha256": card["contract_sha256"]}
                          for c, card in sorted(self.cards.items())}}


def _clause_no(ref: str) -> int:
    return int(ref.lstrip("§")) if ref.lstrip("§").isdigit() else 0


def reconcile(documents: list[dict], lines: list[dict], shipments: list[dict], cards: dict[str, dict]) -> dict:
    return Reconciler(documents, lines, shipments, cards).run()



def load_reconciled(data: dict) -> dict:
    """Restore exact Decimals in a reconciled.json read back from disk (amounts are stored as strings)."""
    def dec(v):
        return None if v is None else D(v)

    for rec in data["lines"]:
        for k in ("billed_total", "contract_amount", "expected", "delta", "base_expected"):
            rec[k] = dec(rec.get(k))
        for f in rec["findings"]:
            f["amount"] = dec(f["amount"])
        for a in rec["adjustments"]:
            a["amount"] = dec(a["amount"])
        for c in ((rec.get("pricing") or {}).get("candidates") or []):
            c["amount"] = dec(c["amount"])
    for f in data["invoice_findings"]:
        f["amount_impact"] = dec(f["amount_impact"])
    for s in data["documents"].values():
        s["billed_discount"] = dec(s["billed_discount"])
        s["expected_discount"] = dec(s["expected_discount"])
        if s["discount_range"]:
            s["discount_range"] = {k: dec(v) for k, v in s["discount_range"].items()}
    return data
