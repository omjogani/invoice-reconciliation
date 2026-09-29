import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recon.ingest import IngestError, ingest
from tests.test_parsers import ALPINE, FALCON_CREDIT, FALCON_INVOICE, SAGAR_CREDIT, SAGAR_INVOICE

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp / name).write_text(text, encoding="utf-8")
    return tmp


class IngestTest(unittest.TestCase):
    def test_all_formats_tie_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _write(Path(tmp), {"f.txt": FALCON_INVOICE, "cn.txt": FALCON_CREDIT, "a.json": ALPINE,
                                   "SAGAR-T.csv": SAGAR_INVOICE, "SAGAR-CN-T.csv": SAGAR_CREDIT})
            documents, lines, inventory = ingest(d)
        self.assertEqual(len(documents), 5)
        self.assertEqual(len(lines), 2 + 1 + 2 + 2 + 1)
        self.assertEqual(inventory["unknown"], [])
        self.assertTrue(all(len(e["sha256"]) == 64 for e in inventory["files"]))

    def test_tie_out_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _write(Path(tmp), {"f.txt": FALCON_INVOICE.replace("INVOICE TOTAL: Rs 40,176.00",
                                                                   "INVOICE TOTAL: Rs 40,177.00")})
            with self.assertRaisesRegex(IngestError, "does not tie out"):
                ingest(d)

    def test_alpine_line_count_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _write(Path(tmp), {"a.json": ALPINE.replace('"consignment_count": 2', '"consignment_count": 3')})
            with self.assertRaisesRegex(IngestError, "line count"):
                ingest(d)

    def test_unknown_format_is_listed_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _write(Path(tmp), {"f.txt": FALCON_INVOICE, "mystery.xml": "<invoice/>"})
            documents, _, inventory = ingest(d)
        self.assertEqual(inventory["unknown"], ["mystery.xml"])
        self.assertEqual([e["status"] for e in inventory["files"]], ["parsed", "unknown"])

    def test_duplicate_document_id_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _write(Path(tmp), {"f1.txt": FALCON_INVOICE, "f2.txt": FALCON_INVOICE})
            with self.assertRaisesRegex(IngestError, "more than once"):
                ingest(d)


class SampleDataTest(unittest.TestCase):
    """Structural checks on the real inputs: nothing lost, everything ties out."""

    def test_sample_documents(self):
        documents, lines, inventory = ingest(ROOT / "data" / "invoices")
        self.assertEqual(len(inventory["files"]), 17)
        self.assertEqual(inventory["unknown"], [])
        self.assertEqual(len(documents), 17)
        self.assertEqual(len(lines), 380)
        self.assertEqual(len({l["line_key"] for l in lines}), 380)
        self.assertEqual(sum(d["doc_type"] == "credit_note" for d in documents), 2)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run([sys.executable, "-m", "recon", "ingest", "--invoices",
                                  str(ROOT / "data" / "invoices"), "--out", tmp],
                                 cwd=ROOT, capture_output=True, text=True, check=True)
            summary = json.loads(out.stdout)
            self.assertEqual(summary, {"documents": 17, "lines": 380, "unknown": []})
            for name in ("documents.json", "lines.jsonl", "inventory.json"):
                self.assertTrue((Path(tmp) / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
