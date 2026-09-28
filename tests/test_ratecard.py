import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from recon.ratecard import check_card, compare
from recon.ratecard.contract import contains_quote, numbers_in
from recon.ratecard.evaluate import evaluate_contract
from recon.ratecard.snapshot import SnapshotStore
from tests.helpers import CONTRACTS, ROOT, contract, fixture_card


class QuoteHelpersTest(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(numbers_in("501 kg to 2,000 kg: **₹24.00 per km**"),
                         {Decimal("501"), Decimal("2000"), Decimal("24.00")})
        self.assertIn(Decimal("12"), numbers_in("fuel surcharge of 12%"))

    def test_contains_quote(self):
        text = "1. Rates:\n   - **under 50 kg**: ₹9.50\n   - over 50 kg: ₹8.25"
        self.assertTrue(contains_quote(text, "under 50 kg: ₹9.50"))
        self.assertTrue(contains_quote(text, "under 50 kg: ₹9.50 ... over 50 kg: ₹8.25"))
        self.assertFalse(contains_quote(text, "over 50 kg: ₹8.25 ... under 50 kg"))  # order matters
        self.assertFalse(contains_quote(text, "under 50 kg: ₹9.75"))


class CheckCardTest(unittest.TestCase):
    def test_fixture_cards_pass_against_real_contracts(self):
        for carrier in CONTRACTS:
            with self.subTest(carrier=carrier):
                self.assertEqual(check_card(fixture_card(carrier), contract(carrier), carrier), [])

    def test_schema_violation(self):
        card = fixture_card("falcon")
        card["surcharges"][0]["base"] = "everything"
        self.assertTrue(check_card(card, contract("falcon"), "falcon")[0].startswith("schema:"))

    def test_wrong_carrier_and_hash(self):
        card = fixture_card("falcon")
        card["contract_sha256"] = "f" * 64
        problems = check_card(card, contract("falcon"), "sagar")
        self.assertTrue(any("contract_sha256" in p for p in problems))
        self.assertTrue(any("assigned carrier" in p for p in problems))

    def test_invented_number(self):
        card = fixture_card("falcon")
        card["surcharges"][1]["pct"] = "15"  # quote still says 12%
        problems = check_card(card, contract("falcon"), "falcon")
        self.assertEqual(len(problems), 1)
        self.assertIn("/surcharges/1: value 15", problems[0])

    def test_quote_not_in_contract(self):
        card = fixture_card("falcon")
        card["accessorials"][0]["quote"] = "Residential delivery: ₹250.00 flat"
        self.assertIn("quote not found", check_card(card, contract("falcon"), "falcon")[0])

    def test_quote_from_a_different_clause(self):
        card = fixture_card("falcon")
        card["surcharges"][1]["clause"] = "§2"  # fuel text lives in §3
        self.assertIn("quote not found", check_card(card, contract("falcon"), "falcon")[0])

    def test_invented_clause(self):
        card = fixture_card("sagar")
        card["non_pricing"].append({"clause": "§9", "summary": "invented"})
        self.assertTrue(any("§9" in p or "[9]" in p for p in check_card(card, contract("sagar"), "sagar")))

    def test_dropped_clause(self):
        card = fixture_card("alpine")
        card["non_pricing"] = [n for n in card["non_pricing"] if n["clause"] != "§7"]
        self.assertEqual(check_card(card, contract("alpine"), "alpine"),
                         ["clauses not accounted for by any rule or non_pricing entry: §7"])

    def test_overlapping_bands(self):
        card = fixture_card("alpine")
        card["freight"]["components"][0]["bands"][0]["max_inclusive"] = True
        card["freight"]["components"][0]["bands"][1]["min_inclusive"] = True  # 50 kg in both
        self.assertTrue(any("more than one band" in p for p in check_card(card, contract("alpine"), "alpine")))


class CompareTest(unittest.TestCase):
    def test_wording_and_number_format_ignored(self):
        a, b = fixture_card("falcon"), fixture_card("falcon")
        b["surcharges"][0]["label"] = "express"
        b["surcharges"][0]["quote"] = "carries a premium of 15% of the base freight"
        b["surcharges"][0]["pct"] = "15.00"
        b["freight"]["components"][0]["bands"].reverse()
        b["non_pricing"] = []
        self.assertEqual(compare.diff(a, b), [])

    def test_rate_difference_reported(self):
        a, b = fixture_card("falcon"), fixture_card("falcon")
        b["freight"]["components"][0]["bands"][1]["rate"] = "25.00"
        self.assertEqual(compare.diff(a, b), ['/freight/components/0/bands/1/rate: "24" vs "25"'])

    def test_surcharge_order_matters(self):
        a, b = fixture_card("falcon"), fixture_card("falcon")
        b["surcharges"].reverse()
        self.assertFalse(compare.same(a, b))

    def test_gap_filled_is_a_difference(self):
        a, b = fixture_card("alpine"), fixture_card("alpine")
        b["freight"]["components"][0]["bands"][0]["max_inclusive"] = True  # "50 kg or under"
        self.assertTrue(compare.diff(a, b))


class SnapshotAndVerdictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _approve(self, card):
        return self.store.approve(card, approved_by="tester", approved_at="2026-09-28T00:00:00Z",
                                  source_run="test")

    def test_missing_then_ok_uses_snapshot_card(self):
        a, b = fixture_card("sagar"), fixture_card("sagar")
        b["surcharges"][0]["quote"] = "a premium of 20% on the freight charge"
        first = evaluate_contract(a, b, contract("sagar"), "sagar", self.store)
        self.assertEqual(first["verdict"], "needs_approval")
        self._approve(first["candidate"])
        second = evaluate_contract(b, a, contract("sagar"), "sagar", self.store)
        self.assertEqual(second["verdict"], "ok")
        self.assertEqual(second["card"], a)  # the approved wording, not this run's

    def test_disagreement_holds(self):
        a, b = fixture_card("falcon"), fixture_card("falcon")
        b["accessorials"][0]["when"] = {"always": True}
        result = evaluate_contract(a, b, contract("falcon"), "falcon", self.store)
        self.assertEqual(result["verdict"], "hold")
        self.assertTrue(result["differences"])

    def test_snapshot_drift_holds(self):
        a = fixture_card("falcon")
        self._approve(a)
        drifted = fixture_card("falcon")
        drifted["dispute_window"] = None
        drifted["non_pricing"].append({"clause": "§7", "summary": "disputes"})
        result = evaluate_contract(drifted, drifted, contract("falcon"), "falcon", self.store)
        self.assertEqual(result["verdict"], "hold")
        self.assertIn("differs from the approved snapshot", result["reasons"][0])

    def test_unsupported_holds(self):
        a = fixture_card("alpine")
        a["unsupported"] = [{"clause": "§7", "quote": "Rate revisions require 60 days' written notice",
                             "why": "time-dependent rates"}]
        result = evaluate_contract(a, a, contract("alpine"), "alpine", self.store)
        self.assertEqual(result["verdict"], "hold")

    def test_invalid_card_holds(self):
        bad = fixture_card("alpine")
        bad["invoice_discounts"][0]["pct"] = "10"
        result = evaluate_contract(bad, fixture_card("alpine"), contract("alpine"), "alpine", self.store)
        self.assertEqual(result["verdict"], "hold")
        self.assertTrue(result["reasons"][0].startswith("extraction A:"))

    def test_approval_does_not_overwrite(self):
        self._approve(fixture_card("alpine"))
        with self.assertRaises(FileExistsError):
            self._approve(fixture_card("alpine"))


class ApproveCliTest(unittest.TestCase):
    def _run(self, card: dict, store: str):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(card, fh)
        return subprocess.run([sys.executable, "-m", "recon", "approve-rate-card", "--card", fh.name,
                               "--contract", str(CONTRACTS["sagar"]), "--carrier", "sagar",
                               "--approved-by", "tester", "--source-run", "test", "--store", store],
                              cwd=ROOT, capture_output=True, text=True)

    def test_approves_valid_and_refuses_invalid(self):
        with tempfile.TemporaryDirectory() as store:
            bad = fixture_card("sagar")
            bad["freight"]["components"][0]["bands"][0]["rate"] = "7.00"
            refused = self._run(bad, store)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("refusing to approve", refused.stderr)
            ok = self._run(fixture_card("sagar"), store)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertTrue(list(Path(store).glob("sagar/*.json")))


if __name__ == "__main__":
    unittest.main()
