import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from recon.ingest import ingest
from recon.matching import ShipmentIndex
from recon.model import make_document, make_line, write_json, write_jsonl
from recon.reconcile import reconcile
from tests.helpers import ROOT, fixture_cards


def ship(sid, ref, carrier, date, kg, km, service="standard", handling=(), status="delivered"):
    return {"shipment_id": sid, "carrier_consignment_ref": ref, "carrier": carrier, "ship_date": date,
            "distance_km": km, "billed_weight_kg": kg, "service_level": service,
            "special_handling": list(handling), "delivery_status": status,
            "delivered_at": date if status == "delivered" else None}


def doc(doc_id, carrier, total, *, doc_type="invoice", start=None, end=None, discount=None, against=None,
        doc_date=None):
    return make_document(doc_id=doc_id, doc_type=doc_type, carrier=carrier, source_file=f"{doc_id}.x",
                         format="test", period_start=start, period_end=end, doc_date=doc_date,
                         printed_total=total, printed_discount=discount, references_invoice=against)


def line(doc_id, pos, ref, carrier, total, components=(), printed=None, doc_type="invoice", against=None):
    return make_line(doc_id=doc_id, doc_type=doc_type, carrier=carrier, position=pos, consignment_ref=ref,
                     billed_total=total, billed_components=list(components), printed=printed or {},
                     references_invoice=against, source={"file": f"{doc_id}.x", "line": pos})


def by_key(result):
    return {l["line_key"]: l for l in result["lines"]}


def codes(rec):
    return [f["code"] for f in rec["findings"] if not f["resolved_by"]]


class FalconScenarios(unittest.TestCase):
    cards = fixture_cards()

    def test_clean_line_accepted(self):
        s = [ship("S1", "FF-1", "falcon", "2026-07-02", 900, 650)]
        r = reconcile([doc("F-A", "falcon", "17472.00", start="2026-07-01", end="2026-07-14")],
                      [line("F-A", 1, "FF-1", "falcon", "17472.00",
                            [{"label": "Freight incl. fuel surcharge", "kind": "freight", "amount": "17472.00"}],
                            {"distance_km": "650", "weight_kg": "900", "service_level": "standard"})],
                      s, self.cards)
        rec = r["lines"][0]
        self.assertEqual((rec["disposition"], rec["expected"], rec["delta"]), ("accept", Decimal("17472.00"), 0))
        self.assertEqual(rec["contract_clause"], "falcon-freight.md §1, §3")
        self.assertEqual(rec["shipment_id"], "S1")

    def test_unauthorised_detention_charge(self):
        s = [ship("S1", "FF-1", "falcon", "2026-07-02", 700, 800)]
        r = reconcile([doc("F-A", "falcon", "22704.00", start="2026-07-01", end="2026-07-14")],
                      [line("F-A", 1, "FF-1", "falcon", "22704.00",
                            [{"label": "Detention charge at consignee", "kind": "accessorial", "amount": "1200.00"},
                             {"label": "Freight incl. fuel surcharge", "kind": "freight", "amount": "21504.00"}])],
                      s, self.cards)
        rec = r["lines"][0]
        self.assertEqual(codes(rec), ["UNAUTHORISED_ACCESSORIAL"])
        self.assertEqual((rec["disposition"], rec["delta"]), ("dispute", Decimal("1200.00")))
        self.assertIn("§5", rec["contract_clause"])

    def test_fuel_overcharge_is_a_rate_mismatch(self):
        s = [ship("S1", "FF-1", "falcon", "2026-08-20", 1800, 180)]
        r = reconcile([doc("F-B", "falcon", "4968.00", start="2026-08-15", end="2026-08-30")],
                      [line("F-B", 1, "FF-1", "falcon", "4968.00",
                            [{"label": "Freight incl. fuel surcharge", "kind": "freight", "amount": "4968.00"}])],
                      s, self.cards)
        rec = r["lines"][0]
        self.assertEqual(codes(rec), ["RATE_MISMATCH"])
        self.assertEqual(rec["delta"], Decimal("129.60"))

    def test_duplicate_in_period_rule(self):
        s = [ship("S3", "FF-3", "falcon", "2026-07-04", 900, 275)]
        comps = [{"label": "Freight incl. fuel surcharge", "kind": "freight", "amount": "7392.00"}]
        printed = {"distance_km": "275", "weight_kg": "900", "service_level": "standard"}
        docs = [doc("F-07A", "falcon", "7392.00", start="2026-07-01", end="2026-07-14"),
                doc("F-07B", "falcon", "7392.00", start="2026-07-15", end="2026-07-30")]
        lines = [line("F-07B", 1, "FF-3", "falcon", "7392.00", comps, printed),  # listed first on purpose
                 line("F-07A", 1, "FF-3", "falcon", "7392.00", comps, printed)]
        r = by_key(reconcile(docs, lines, s, self.cards))
        kept, dup = r["F-07A#1"], r["F-07B#1"]
        self.assertEqual(kept["disposition"], "accept")
        self.assertEqual(codes(dup), ["OUT_OF_PERIOD", "DUPLICATE_BILLING"])
        self.assertEqual((dup["disposition"], dup["expected"], dup["delta"], dup["shipment_id"]),
                         ("dispute", Decimal("0"), Decimal("7392.00"), "S3"))

    def test_duplicate_with_different_details_escalates(self):
        s = [ship("S3", "FF-3", "falcon", "2026-07-04", 900, 275)]
        docs = [doc("F-07A", "falcon", "7392.00", start="2026-07-01", end="2026-07-14"),
                doc("F-07B", "falcon", "7500.00", start="2026-07-15", end="2026-07-30")]
        lines = [line("F-07A", 1, "FF-3", "falcon", "7392.00"), line("F-07B", 1, "FF-3", "falcon", "7500.00")]
        dup = by_key(reconcile(docs, lines, s, self.cards))["F-07B#1"]
        self.assertEqual((dup["disposition"], dup["expected"]), ("escalate", None))

    def test_credit_fully_corrects(self):
        s = [ship("S5", "FF-5", "falcon", "2026-07-06", 260, 800, service="express")]
        docs = [doc("F-07A", "falcon", "19044.00", start="2026-07-01", end="2026-07-14"),
                doc("F-CN", "falcon", "-496.80", doc_type="credit_note", against="F-07A", doc_date="2026-08-05")]
        lines = [line("F-07A", 1, "FF-5", "falcon", "19044.00",
                      [{"label": "Freight incl. fuel surcharge", "kind": "freight", "amount": "19044.00"}]),
                 line("F-CN", 1, "FF-5", "falcon", "-496.80", doc_type="credit_note", against="F-07A")]
        r = by_key(reconcile(docs, lines, s, self.cards))
        orig, cn = r["F-07A#1"], r["F-CN#1"]
        self.assertEqual((orig["expected"], orig["delta"], orig["disposition"]),
                         (Decimal("19044.00"), 0, "accept"))
        self.assertEqual((cn["expected"], cn["delta"], cn["disposition"]), (Decimal("-496.80"), 0, "accept"))
        self.assertEqual(orig["expected"] + cn["expected"], Decimal("18547.20"))  # net = contract amount

    def test_dispute_window_note_uses_assumed_date(self):
        s = [ship("S1", "FF-1", "falcon", "2026-07-02", 700, 800)]
        r = reconcile([doc("F-A", "falcon", "22704.00", start="2026-07-01", end="2026-07-14")],
                      [line("F-A", 1, "FF-1", "falcon", "22704.00",
                            [{"label": "Detention", "kind": "accessorial", "amount": "1200.00"},
                             {"label": "Freight", "kind": "freight", "amount": "21504.00"}])], s, self.cards)
        note = [n for n in r["lines"][0]["notes"] if n.startswith("Dispute window")][0]
        self.assertIn("closes 2026-08-28 if the invoice is dated 2026-07-14", note)
        self.assertIn("assumed", note)
        self.assertEqual(r["documents"]["F-A"]["dispute_window"]["closes"], "2026-08-28")


class SagarScenarios(unittest.TestCase):
    cards = fixture_cards()

    def test_arithmetic_error(self):
        s = [ship("S3", "SG-3", "sagar", "2026-07-04", 60, 85)]
        r = reconcile([doc("S-1", "sagar", "570.00")],
                      [line("S-1", 1, "SG-3", "sagar", "570.00",
                            [{"label": "freight_rs", "kind": "freight", "amount": "530.00"},
                             {"label": "chill_prem_rs", "kind": "surcharge", "amount": "0.00"}])], s, self.cards)
        rec = r["lines"][0]
        self.assertEqual(codes(rec), ["ARITHMETIC_ERROR"])
        self.assertEqual((rec["delta"], rec["disposition"]), (Decimal("40.00"), "dispute"))

    def test_partial_credit(self):
        s = [ship("S56", "SG-56", "sagar", "2026-08-07", 260, 60)]
        docs = [doc("S-AUG", "sagar", "1940.00"),
                doc("S-CN", "sagar", "-156.00", doc_type="credit_note", against="S-AUG")]
        lines = [line("S-AUG", 1, "SG-56", "sagar", "1940.00",
                      [{"label": "freight_rs", "kind": "freight", "amount": "1940.00"},
                       {"label": "chill_prem_rs", "kind": "surcharge", "amount": "0.00"}]),
                 line("S-CN", 1, "SG-56", "sagar", "-156.00", doc_type="credit_note", against="S-AUG")]
        r = by_key(reconcile(docs, lines, s, self.cards))
        orig, cn = r["S-AUG#1"], r["S-CN#1"]
        self.assertEqual((orig["expected"], orig["delta"], orig["disposition"]),
                         (Decimal("1836.00"), Decimal("104.00"), "dispute"))
        self.assertEqual(codes(orig), ["PARTIAL_CREDIT"])
        self.assertEqual(orig["contract_clause"], "sagar-roadlines.md §1, §6")
        self.assertEqual((cn["disposition"], cn["delta"]), ("accept", 0))

    def test_unmatched_with_in_transit_near_miss(self):
        s = [ship("S69", "SG-7069", "sagar", "2026-08-07", 110, 88, status="in_transit")]
        r = reconcile([doc("S-AUG", "sagar", "836.00")],
                      [line("S-AUG", 1, "SG-7969", "sagar", "836.00",
                            [{"label": "freight_rs", "kind": "freight", "amount": "836.00"}],
                            {"booking_date": "2026-08-07", "weight_kg": "110", "distance_km": "88"})],
                      s, self.cards)
        rec = r["lines"][0]
        self.assertEqual((rec["shipment_id"], rec["expected"], rec["delta"], rec["disposition"]),
                         (None, None, None, "escalate"))
        self.assertEqual(rec["contract_clause"], "sagar-roadlines.md §5")
        self.assertEqual(rec["candidates"][0]["consignment_ref"], "SG-7069")
        self.assertIn("in_transit", rec["notes"][0])
        self.assertIn("Not used as a match", rec["notes"][0])

    def test_credit_without_overbill_escalates(self):
        s = [ship("S1", "SG-1", "sagar", "2026-07-02", 260, 60)]
        docs = [doc("S-1", "sagar", "1680.00"), doc("S-CN", "sagar", "-50.00", doc_type="credit_note", against="S-1")]
        lines = [line("S-1", 1, "SG-1", "sagar", "1680.00"),
                 line("S-CN", 1, "SG-1", "sagar", "-50.00", doc_type="credit_note", against="S-1")]
        cn = by_key(reconcile(docs, lines, s, self.cards))["S-CN#1"]
        self.assertEqual((codes(cn), cn["disposition"]), (["CREDIT_WITHOUT_OVERBILL"], "escalate"))


class AlpineScenarios(unittest.TestCase):
    cards = fixture_cards()

    def _month(self, n_tendered, lines, discount):
        shipments = [ship(f"S{i}", f"AE-{i}", "alpine", "2026-07-10", 30, 100) for i in range(n_tendered)]
        shipments.append(ship("S50", "AE-50", "alpine", "2026-07-11", 50.0, 100))
        total = sum(Decimal(l["billed_total"]) for l in lines) - Decimal(discount)
        return reconcile([doc("A-07", "alpine", str(total), start="2026-07-01", end="2026-07-31",
                              discount=discount)], lines, shipments, self.cards)

    def test_fifty_kg_escalated_with_candidates(self):
        r = self._month(3, [line("A-07", 1, "AE-50", "alpine", "475.00")], "0")
        rec = r["lines"][0]
        self.assertEqual((rec["expected"], rec["delta"], rec["disposition"]), (None, None, "escalate"))
        self.assertEqual(codes(rec), ["CONTRACT_GAP"])
        self.assertIn("₹475.00", rec["notes"][0])
        self.assertIn("₹412.50", rec["notes"][0])

    def test_missing_discount_on_determinable_lines(self):
        lines = [line("A-07", i + 1, f"AE-{i}", "alpine", "285.00") for i in range(13)]
        lines.append(line("A-07", 14, "AE-50", "alpine", "475.00"))
        r = self._month(13, lines, "0")
        [f] = r["invoice_findings"]
        self.assertEqual((f["code"], f["disposition"]), ("MISSING_DISCOUNT", "dispute"))
        self.assertEqual(f["amount_impact"], Decimal("185.25"))  # 5% of 13 x 285.00
        self.assertIn("₹20.63 to ₹23.75", f["notes"][0])
        self.assertIsNone(r["documents"]["A-07"]["expected_discount"])

    def test_discount_applied_correctly_raises_nothing(self):
        lines = [line("A-07", i + 1, f"AE-{i}", "alpine", "285.00") for i in range(13)]
        r = self._month(13, lines, "185.25")
        self.assertEqual(r["invoice_findings"], [])
        self.assertEqual(r["documents"]["A-07"]["expected_discount"], Decimal("185.25"))

    def test_threshold_is_strictly_more_than(self):
        lines = [line("A-07", i + 1, f"AE-{i}", "alpine", "285.00") for i in range(11)]
        r = self._month(11, lines, "0")  # 11 + the 50 kg shipment = 12 tendered
        self.assertEqual(r["invoice_findings"], [])


class MatchingTest(unittest.TestCase):
    def test_exact_only_and_case_insensitive(self):
        idx = ShipmentIndex([ship("S1", "FF-1", "falcon", "2026-07-01", 1, 1)])
        self.assertEqual(idx.match("falcon", " ff-1 ")["shipment_id"], "S1")
        self.assertIsNone(idx.match("falcon", "FF-11"))
        self.assertIsNone(idx.match("sagar", "FF-1"))


class SampleDataInvariantsTest(unittest.TestCase):
    """Structural invariants on the real inputs, priced with the test fixture cards."""

    def test_invariants(self):
        documents, lines, _ = ingest(ROOT / "data" / "invoices")
        shipments = json.loads((ROOT / "data" / "shipments.json").read_text())
        r = reconcile(documents, lines, shipments, fixture_cards())
        self.assertEqual(len(r["lines"]), 380)
        for rec in r["lines"]:
            if rec["expected"] is None:
                self.assertIsNone(rec["delta"], rec["line_key"])
                self.assertEqual(rec["disposition"], "escalate", rec["line_key"])
            else:
                self.assertEqual(rec["delta"], rec["billed_total"] - rec["expected"], rec["line_key"])
            if rec["disposition"] != "accept":
                self.assertTrue(rec["findings"], rec["line_key"])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            documents, lines, _ = ingest(ROOT / "data" / "invoices")
            write_json(tmp / "documents.json", documents)
            write_jsonl(tmp / "lines.jsonl", lines)
            write_json(tmp / "cards.json", fixture_cards())
            out = subprocess.run([sys.executable, "-m", "recon", "reconcile", "--work", str(tmp),
                                  "--shipments", str(ROOT / "data" / "shipments.json"),
                                  "--cards", str(tmp / "cards.json"), "--out", str(tmp / "reconciled.json")],
                                 cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["lines"], 380)


if __name__ == "__main__":
    unittest.main()
