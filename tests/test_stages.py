import json
import tempfile
import unittest
from pathlib import Path

from recon.model import read_json, write_json
from recon.ratecard.snapshot import SnapshotStore
from recon.stages import compare_cards, finalize_review, plan_compile, rates_check, vocabulary
from tests.helpers import CONTRACTS, ROOT, fixture_card
from tests.test_review_report import Pipeline, stub_review

CARRIERS = json.loads((ROOT / "config" / "carriers.json").read_text())["carriers"]
SHIPMENTS = json.loads((ROOT / "data" / "shipments.json").read_text())


class CompileStagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _card(self, name, card):
        path = self.t / name
        write_json(path, {"_session_id": "worker", **card})
        return path

    def test_vocabulary_from_shipments(self):
        v = vocabulary(SHIPMENTS)
        self.assertEqual(v["service_level"], ["express", "standard"])
        self.assertEqual(v["special_handling"], ["cold_chain", "fragile", "residential"])

    def test_plan_compile(self):
        jobs = plan_compile(CARRIERS, ROOT, self.t / "vocabulary.json")
        self.assertEqual([j["carrier"] for j in jobs], ["alpine", "falcon", "sagar"])
        self.assertTrue(all(len(j["contract_sha256"]) == 64 and isinstance(j["contract_path"], str) for j in jobs))

    def test_compare_agree_ignores_worker_metadata(self):
        a = self._card("a.json", fixture_card("sagar"))
        b = self._card("b.json", fixture_card("sagar"))
        report = compare_cards(a, b, CONTRACTS["sagar"], "sagar", vocabulary(SHIPMENTS))
        self.assertTrue(report["agree"], report)

    def test_compare_reports_problems_and_differences(self):
        bad = fixture_card("sagar")
        bad["surcharges"][0]["when"] = {"special_handling": "cold chain"}  # not the shipment vocabulary
        report = compare_cards(self._card("a.json", fixture_card("sagar")), self._card("b.json", bad),
                               CONTRACTS["sagar"], "sagar", vocabulary(SHIPMENTS))
        self.assertFalse(report["agree"])
        self.assertTrue(any("cold chain" in p for p in report["problems_b"]))
        other = fixture_card("sagar")
        other["freight"]["components"][0]["bands"][0]["rate"] = "6.50"
        other["freight"]["components"][0]["quote"] = "a weight component of ₹6.00 per kg of billed weight"
        report = compare_cards(self._card("a.json", fixture_card("sagar")), self._card("c.json", other),
                               CONTRACTS["sagar"], "sagar")
        self.assertFalse(report["agree"])

    def test_rates_check_needs_approval_then_ok(self):
        results = {}
        for c in CARRIERS:
            a = self._card(f"{c['carrier']}-a.json", fixture_card(c["carrier"]))
            b = self._card(f"{c['carrier']}-b.json", fixture_card(c["carrier"]))
            path = self.t / f"{c['carrier']}-result.json"
            write_json(path, {"carrier": c["carrier"], "card_a": str(a), "card_b": str(b), "run_dir": "test"})
            results[c["carrier"]] = str(path)
        store = self.t / "approved"
        first = rates_check(results, CARRIERS, ROOT, store, self.t / "rates1")
        self.assertEqual(first["verdict"], "needs_approval")
        self.assertFalse((self.t / "rates1" / "rate-cards.json").exists())
        for c in first["contracts"]:
            SnapshotStore(store).approve(read_json(c["candidate_path"]), approved_by="tester",
                                         approved_at="2026-09-28T00:00:00Z", source_run="test")
        second = rates_check(results, CARRIERS, ROOT, store, self.t / "rates2")
        self.assertEqual(second["verdict"], "ok")
        cards = read_json(self.t / "rates2" / "rate-cards.json")
        self.assertEqual(sorted(cards), ["alpine", "falcon", "sagar"])
        self.assertNotIn("_session_id", cards["falcon"])


class FinalizeReviewTest(unittest.TestCase):
    p = Pipeline()

    def test_latest_passing_attempt_wins_else_failure(self):
        batch = self.p.batches[0]
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            write_json(t / "batch.json", batch)
            bad = stub_review(batch)
            bad["items"][0]["disposition"] = "accept"
            write_json(t / "r1.json", bad)
            write_json(t / "r2.json", stub_review(batch))
            result = finalize_review(t / "batch.json", [t / "r1.json", t / "r2.json", t / "r3.json"], t / "final.json")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(read_json(t / "final.json"), read_json(t / "r2.json"))
            result = finalize_review(t / "batch.json", [t / "r1.json"], t / "final.json")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(read_json(t / "final.json")["status"], "failed")


if __name__ == "__main__":
    unittest.main()


class MergeFallbackTest(unittest.TestCase):
    def test_merge_checks_and_ties_out(self):
        from recon.ingest import ingest, write_outputs
        from recon.model import read_jsonl
        from recon.stages import merge_fallback

        doc = {"doc_id": "NEW-1", "doc_type": "invoice", "carrier": "sagar", "source_file": "new.pdf",
               "format": "agent-fallback", "printed_total": "300.00"}
        line = {"doc_id": "NEW-1", "doc_type": "invoice", "carrier": "sagar", "position": 1,
                "consignment_ref": "SG-9", "billed_total": "300.00"}
        with tempfile.TemporaryDirectory() as tmp:
            documents, lines, inventory = ingest(ROOT / "data" / "invoices")
            write_outputs(Path(tmp), documents, lines, inventory)
            bad = merge_fallback({"documents": [{**doc, "printed_total": "301.00"}], "lines": [line]},
                                 Path(tmp), ["sagar"])
            self.assertTrue(any("does not tie out" in p for p in bad))
            self.assertEqual(len(read_jsonl(Path(tmp) / "lines.jsonl")), 380)  # nothing merged
            self.assertEqual(merge_fallback({"documents": [doc], "lines": [line]}, Path(tmp), ["sagar"]), [])
            self.assertEqual(len(read_jsonl(Path(tmp) / "lines.jsonl")), 381)
