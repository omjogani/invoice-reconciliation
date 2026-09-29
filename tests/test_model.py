import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from recon.model import dumps, make_document, make_line, read_json, read_jsonl, write_json, write_jsonl


class RecordTest(unittest.TestCase):
    def test_line_key_is_derived(self):
        line = make_line(doc_id="INV-1", doc_type="invoice", carrier="falcon", position=3,
                         consignment_ref="FF-1", billed_total="10.00")
        self.assertEqual(line["line_key"], "INV-1#3")
        self.assertEqual(line["billed_components"], [])
        self.assertEqual(line["printed"], {})

    def test_missing_and_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            make_line(doc_id="INV-1", doc_type="invoice", carrier="falcon", position=1,
                      billed_total="1")  # no consignment_ref
        with self.assertRaises(ValueError):
            make_line(doc_id="INV-1", doc_type="invoice", carrier="falcon", position=1,
                      consignment_ref="X", billed_total="1", colour="red")
        with self.assertRaises(ValueError):
            make_document(doc_id="X", doc_type="receipt", carrier="c", source_file="f", format="x")


class JsonIoTest(unittest.TestCase):
    def test_deterministic_and_exact(self):
        obj = {"b": Decimal("18547.2"), "a": [Decimal("-496.8")]}
        self.assertEqual(dumps(obj), dumps(dict(reversed(list(obj.items())))))
        self.assertIn('"18547.20"', dumps(obj))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x" / "doc.json"
            write_json(p, {"amount": Decimal("1.5")})
            self.assertEqual(read_json(p), {"amount": "1.50"})
            q = Path(tmp) / "lines.jsonl"
            write_jsonl(q, [{"k": 1}, {"k": Decimal("2")}])
            self.assertEqual(read_jsonl(q), [{"k": 1}, {"k": "2.00"}])


if __name__ == "__main__":
    unittest.main()
