import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recon.ingest import ingest
from recon.model import read_json, write_json, write_jsonl
from recon.money import D, fmt_western
from recon.reconcile import load_reconciled, reconcile
from recon.report import assemble, memo_files, validate_report
from recon.review import check_review, plan_review
from tests.helpers import CONTRACTS, ROOT, fixture_cards

AS_OF = "2026-09-28"


def stub_review(batch: dict, raise_ids=()) -> dict:
    """What a well-behaved reviewer returns. Test-only: real memos are written by agents."""
    items = []
    for item in batch["items"]:
        clause = item["contract_clause"] or ""
        refs = clause.split(" ", 1)[1] if " " in clause else ""
        figures = ", ".join(f"₹{fmt_western(D(f))}" for f in item["required_figures"])
        window = item["dispute_window"]
        memo = (f"# {item['invoice']} {item.get('consignment_ref') or item['item_id']}\n\n"
                f"Amount at issue: {figures or 'not quantifiable'}.\n"
                + (f"Dispute window closes {window['closes']} ({window['status']}).\n" if window else "")
                + "Action: raise with the carrier.\n")
        raised = item["item_id"] in raise_ids
        items.append({"item_id": item["item_id"],
                      "disposition": "escalate" if raised else item["policy_disposition"],
                      "raised_reason": "needs a commercial decision" if raised else None,
                      "justification": f"Per the contract {refs or '(no clause applies)'}, see findings.",
                      "memo_markdown": memo})
    return {"_session_id": "test", "batch_id": batch["batch_id"], "items": items}


class Pipeline:
    """The deterministic pipeline on the real inputs with test fixture cards."""

    def __init__(self):
        self.documents, self.lines, _ = ingest(ROOT / "data" / "invoices")
        shipments = json.loads((ROOT / "data" / "shipments.json").read_text())
        self.reconciled = reconcile(self.documents, self.lines, shipments, fixture_cards())
        self.batches = plan_review(self.reconciled, CONTRACTS, AS_OF, batch_size=5)
        self.items = {i["item_id"]: i for b in self.batches for i in b["items"]}

    def reviews(self, raise_ids=()):
        out = {}
        for b in self.batches:
            for r in stub_review(b, raise_ids)["items"]:
                out[r["item_id"]] = r
        return out


class ReviewPlanningTest(unittest.TestCase):
    p = Pipeline()

    def test_batches_are_bounded_single_carrier_and_cover_every_exception(self):
        for b in self.p.batches:
            self.assertLessEqual(len(b["items"]), 5)
            self.assertEqual({i["carrier"] for i in b["items"]}, {b["carrier"]})
        non_accept = [l["line_key"] for l in self.p.reconciled["lines"] if l["disposition"] != "accept"]
        non_accept += [f["finding_id"] for f in self.p.reconciled["invoice_findings"] if f["disposition"] != "accept"]
        self.assertEqual(sorted(self.p.items), sorted(non_accept))

    def test_items_carry_clause_text_and_window_status(self):
        item = self.p.items["FALCON-2026-07A#11"]
        self.assertIn("§5", item["clause_texts"])
        self.assertIn("No other accessorial charges", item["clause_texts"]["§5"])
        self.assertEqual((item["dispute_window"]["closes"], item["dispute_window"]["status"]), ("2026-08-28", "closed"))


class ReviewCheckTest(unittest.TestCase):
    p = Pipeline()

    def batch(self, carrier="falcon"):
        return next(b for b in self.p.batches if b["carrier"] == carrier)

    def test_good_output_passes(self):
        for b in self.p.batches:
            self.assertEqual(check_review(b, stub_review(b)), [], b["batch_id"])

    def test_lowering_is_rejected(self):
        b = self.batch()
        out = stub_review(b)
        out["items"][0]["disposition"] = "accept"
        self.assertTrue(any("may only raise" in p for p in check_review(b, out)))

    def test_raise_needs_reason(self):
        b = self.batch()
        out = stub_review(b)
        out["items"][0]["disposition"] = "escalate"
        self.assertTrue(any("raised_reason" in p for p in check_review(b, out)))

    def test_amounts_are_not_allowed(self):
        b = self.batch()
        out = stub_review(b)
        out["items"][0]["expected_amount"] = 0
        self.assertTrue(check_review(b, out)[0].startswith("schema:"))

    def test_items_cannot_be_dropped(self):
        b = self.batch()
        out = stub_review(b)
        out["items"].pop()
        self.assertTrue(any("missing from the output" in p for p in check_review(b, out)))

    def test_memo_figures_and_deadline_checked(self):
        b = self.batch()
        out = stub_review(b)
        out["items"][0]["memo_markdown"] = out["items"][0]["memo_markdown"].replace("₹", "Rs ").replace(",", "")
        self.assertEqual(check_review(b, out), [])  # plain digits are fine
        out["items"][0]["memo_markdown"] = f"{b['items'][0]['invoice']} {b['items'][0]['consignment_ref']} " + "x" * 80
        problems = check_review(b, out)
        self.assertTrue(any("does not state the amount" in p for p in problems))
        self.assertTrue(any("dispute-window date" in p for p in problems))

    def test_justification_must_cite_clause(self):
        b = self.batch()
        out = stub_review(b)
        out["items"][0]["justification"] = "The carrier billed something the agreement does not allow."
        self.assertTrue(any("cites none of the governing clauses" in p for p in check_review(b, out)))


class AssembleAndValidateTest(unittest.TestCase):
    p = Pipeline()

    def build(self, raise_ids=()):
        reviews = self.p.reviews(raise_ids)
        report = assemble(self.p.reconciled, self.p.documents, reviews)
        memos = memo_files(self.p.reconciled, reviews, self.p.items)
        return report, memos

    def validate(self, report, memos):
        return validate_report(report, parsed_lines=self.p.lines, documents=self.p.documents,
                               reconciled=self.p.reconciled, items=self.p.items, memos=memos)

    def test_valid_report(self):
        report, memos = self.build()
        self.assertEqual(self.validate(report, memos), [])
        s = report["summary"]
        self.assertEqual(s["line_count"], 380)
        self.assertEqual(sum(s["counts_by_disposition"].values()), 380)
        self.assertIsNone(s["total_expected"])  # a contract gap makes it undeterminable (D2)
        self.assertEqual(len(memos), len(self.p.items))

    def test_payable_and_held_note(self):
        report, _ = self.build()
        t = next(t for t in report["invoice_totals"] if t["invoice"] == "FALCON-2026-07B")
        self.assertIn("Net payable now ₹198,135.20", t["notes"])
        self.assertIn("held ₹7,392.00", t["notes"])

    def test_undeterminable_invoice_gives_range(self):
        report, _ = self.build()
        t = next(t for t in report["invoice_totals"] if t["invoice"] == "ALPINE-0726")
        self.assertIsNone(t["expected_total"])
        self.assertIn("ALPINE-0726#5", t["notes"])

    def test_raised_disposition_flows_through(self):
        report, memos = self.build(raise_ids={"FALCON-2026-07A#11"})
        self.assertEqual(self.validate(report, memos), [])
        line = next(l for l in report["lines"] if l["invoice"] == "FALCON-2026-07A" and l["consignment_ref"] == "FF-8011")
        self.assertEqual(line["disposition"], "escalate")
        self.assertIn("Reviewer raised", line["notes"])

    def test_tampering_is_caught(self):
        report, memos = self.build()
        cases = {
            "delta": lambda r, m: r["lines"][0].__setitem__("delta", 1.0),
            "missing line": lambda r, m: r["lines"].pop(),
            "in dispute": lambda r, m: r["summary"].__setitem__("total_in_dispute", 1.0),
            "expected total": lambda r, m: next(t for t in r["invoice_totals"] if t["expected_total"] is not None)
            .__setitem__("expected_total", 1.0),
            "memo": lambda r, m: m.pop(sorted(m)[0]),
            "accept unpriced": lambda r, m: next(l for l in r["lines"] if l["expected_amount"] is None)
            .__setitem__("disposition", "accept"),
        }
        for name, tamper in cases.items():
            with self.subTest(name):
                r, m = copy.deepcopy(report), dict(memos)
                tamper(r, m)
                self.assertTrue(self.validate(r, m), name)


class CliDryRunTest(unittest.TestCase):
    """The full CLI chain with stub reviews; nothing is published to the repository."""

    def run_cli(self, *args):
        out = subprocess.run([sys.executable, "-m", "recon", *args], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def test_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self.run_cli("ingest", "--invoices", str(ROOT / "data/invoices"), "--out", str(t / "work"))
            write_json(t / "cards.json", fixture_cards())
            self.run_cli("reconcile", "--work", str(t / "work"), "--shipments", str(ROOT / "data/shipments.json"),
                         "--cards", str(t / "cards.json"), "--out", str(t / "reconciled.json"))
            planned = self.run_cli("plan-review", "--reconciled", str(t / "reconciled.json"), "--as-of", AS_OF,
                                   "--out-dir", str(t / "batches"), "--flowstate-var", "review_jobs",
                                   "--flowstate-count-var", "review_state")
            self.assertIn("FLOWSTATE_OUTPUT_review_jobs=", planned.stdout)
            self.assertIn('FLOWSTATE_OUTPUT_review_state="some"', planned.stdout)
            mapping = {}
            for batch_path in sorted((t / "batches").glob("B*.json")):
                batch = read_json(batch_path)
                write_json(t / "reviews" / batch_path.name, stub_review(batch))
                mapping[batch["batch_id"]] = str(t / "reviews" / batch_path.name)
            write_json(t / "reviews.json", mapping)
            self.run_cli("review-check", "--batches-dir", str(t / "batches"), "--reviews", str(t / "reviews.json"),
                         "--feedback", str(t / "feedback.json"))
            self.run_cli("assemble", "--reconciled", str(t / "reconciled.json"), "--work", str(t / "work"),
                         "--batches-dir", str(t / "batches"), "--reviews", str(t / "reviews.json"),
                         "--out-dir", str(t / "out"))
            self.run_cli("validate-report", "--out-dir", str(t / "out"), "--work", str(t / "work"),
                         "--reconciled", str(t / "reconciled.json"), "--batches-dir", str(t / "batches"))
            (t / "published").mkdir()
            self.run_cli("publish", "--source", str(t / "out"), "--target", str(t / "published"))
            self.assertTrue((t / "published" / "reconciliation-report.json").exists())
            self.assertTrue(list((t / "published" / "memos").glob("*.md")))
            self.run_cli("diff-runs", str(t / "out"), str(t / "published"))


if __name__ == "__main__":
    unittest.main()
