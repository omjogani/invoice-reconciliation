import tempfile
import textwrap
import unittest
from decimal import Decimal
from pathlib import Path

from recon.parsers import ParseError, alpine_json, detect, falcon_text, sagar_csv

FALCON_INVOICE = textwrap.dedent("""\
    FALCON FREIGHT PVT LTD
    Servicing BlueFin Commerce under agreement FF/BFC/2024-11
    TAX INVOICE FALCON-TEST-A    Period: 1-14 2026-07
    ================================================================

    1. Consignment FF-1
       Mumbai to Hyderabad, 650 km, 900 kg, standard
       Freight incl. fuel surcharge: Rs 17,472.00
       LINE TOTAL: Rs 17,472.00

    2. Consignment FF-2
       Mumbai to Bengaluru, 800 km, 700 kg, express
       Detention charge at consignee: Rs 1,200.00
       Freight incl. fuel surcharge: Rs 21,504.00
       LINE TOTAL: Rs 22,704.00

    ================================================================
    INVOICE TOTAL: Rs 40,176.00
    Payment due 30 days from invoice date. E&OE.
    """)

FALCON_CREDIT = textwrap.dedent("""\
    FALCON FREIGHT PVT LTD
    CREDIT NOTE FALCON-CN-T    Date: 2026-08-05
    Against: TAX INVOICE FALCON-TEST-A
    ================================================================

    1. Consignment FF-2
       Correction: fuel surcharge invoiced at 15% in error; contractual
       rate is 12%. Credit for the difference.
       LINE TOTAL: Rs -496.80

    ================================================================
    CREDIT NOTE TOTAL: Rs -496.80
    """)

ALPINE = """{"carrier": "Alpine Express Logistics", "invoice_no": "ALPINE-T", "billing_period": "2026-02",
 "consignment_count": 2, "lines": [
  {"sl": 1, "consignment_no": "AE-1", "booking_date": "2026-02-02", "actual_weight_kg": 18,
   "chargeable_weight_kg": 25.0, "rate_per_kg": 9.5, "handling_fee": 0, "line_amount": 237.5},
  {"sl": 2, "consignment_no": "AE-2", "booking_date": "2026-02-03", "actual_weight_kg": 71.5,
   "chargeable_weight_kg": 71.5, "rate_per_kg": 8.25, "handling_fee": 150, "line_amount": 739.88}],
 "discount": 48.87, "invoice_total": 928.51}"""

SAGAR_INVOICE = textwrap.dedent("""\
    cnote_no,booking_dt,wt_kg,dist_km,freight_rs,chill_prem_rs,total_rs
    SG-1,2026-07-02,260,60,1680.00,0.00,1680.00
    SG-2,2026-07-04,60,85,530.00,0.00,570.00
    TOTAL,,,,,,2250.00
    """)

SAGAR_CREDIT = textwrap.dedent("""\
    credit_note,against_invoice,cnote_no,credit_rs
    SAGAR-CN-T,SAGAR-T,SG-1,-156.00
    TOTAL,,,-156.00
    """)


def _run(module, name, text):
    return module.parse(Path(name), text, "0" * 64)


class FalconTextTest(unittest.TestCase):
    def test_invoice(self):
        self.assertTrue(detect(Path("x.txt"), FALCON_INVOICE) is falcon_text)
        doc, lines = _run(falcon_text, "x.txt", FALCON_INVOICE)
        self.assertEqual((doc["doc_id"], doc["doc_type"]), ("FALCON-TEST-A", "invoice"))
        self.assertEqual((doc["period_start"], doc["period_end"]), ("2026-07-01", "2026-07-14"))
        self.assertEqual(doc["printed_total"], Decimal("40176.00"))
        self.assertEqual([l["line_key"] for l in lines], ["FALCON-TEST-A#1", "FALCON-TEST-A#2"])
        second = lines[1]
        self.assertEqual(second["billed_total"], Decimal("22704.00"))
        self.assertEqual(second["billed_components"][0], {"label": "Detention charge at consignee",
                                                           "kind": "accessorial", "amount": Decimal("1200.00")})
        self.assertEqual(second["billed_components"][1]["kind"], "freight")
        self.assertEqual(second["printed"]["service_level"], "express")
        self.assertEqual(second["printed"]["distance_km"], "800")
        self.assertEqual(second["source"], {"file": "x.txt", "line": 11})

    def test_credit_note(self):
        doc, lines = _run(falcon_text, "cn.txt", FALCON_CREDIT)
        self.assertEqual((doc["doc_type"], doc["doc_date"], doc["references_invoice"]),
                         ("credit_note", "2026-08-05", "FALCON-TEST-A"))
        self.assertEqual(lines[0]["billed_total"], Decimal("-496.80"))
        self.assertEqual(lines[0]["references_invoice"], "FALCON-TEST-A")
        self.assertIn("15% in error", lines[0]["reason"])
        self.assertEqual(lines[0]["billed_components"], [])

    def test_unexpected_invoice_text_is_an_error(self):
        drifted = FALCON_INVOICE.replace("   Freight incl. fuel surcharge: Rs 17,472.00",
                                         "   Freight incl. fuel surcharge: Rs 17,472.00\n   Toll receipts attached")
        with self.assertRaises(ParseError):
            _run(falcon_text, "x.txt", drifted)

    def test_missing_line_total_is_an_error(self):
        broken = FALCON_INVOICE.replace("   LINE TOTAL: Rs 17,472.00\n", "")
        with self.assertRaises(ParseError):
            _run(falcon_text, "x.txt", broken)


class AlpineJsonTest(unittest.TestCase):
    def test_invoice(self):
        self.assertTrue(detect(Path("a.json"), ALPINE) is alpine_json)
        doc, lines = _run(alpine_json, "a.json", ALPINE)
        self.assertEqual((doc["period_start"], doc["period_end"]), ("2026-02-01", "2026-02-28"))
        self.assertEqual(doc["printed_discount"], Decimal("48.87"))
        self.assertEqual(doc["printed_line_count"], 2)
        self.assertEqual(lines[1]["billed_total"], Decimal("739.88"))
        freight, handling = lines[1]["billed_components"]
        self.assertEqual(freight["amount"], Decimal("589.88"))  # 71.5 x 8.25 = 589.875, half-up
        self.assertTrue(freight["derived"])
        self.assertEqual(handling["amount"], Decimal("150"))
        self.assertEqual((freight["kind"], handling["kind"]), ("freight", "accessorial"))
        self.assertEqual(len(lines[0]["billed_components"]), 1)  # no zero handling component


class SagarCsvTest(unittest.TestCase):
    def test_invoice(self):
        self.assertTrue(detect(Path("SAGAR-T.csv"), SAGAR_INVOICE) is sagar_csv)
        doc, lines = _run(sagar_csv, "SAGAR-T.csv", SAGAR_INVOICE)
        self.assertEqual(doc["doc_id"], "SAGAR-T")
        self.assertIsNone(doc["period_start"])
        self.assertEqual(doc["printed_total"], Decimal("2250.00"))
        self.assertEqual(lines[1]["billed_total"], Decimal("570.00"))
        self.assertEqual([c["amount"] for c in lines[1]["billed_components"]],
                         [Decimal("530.00"), Decimal("0.00")])
        self.assertEqual([c["kind"] for c in lines[1]["billed_components"]], ["freight", "surcharge"])
        self.assertEqual(lines[1]["source"]["line"], 3)

    def test_credit_note(self):
        doc, lines = _run(sagar_csv, "SAGAR-CN-T.csv", SAGAR_CREDIT)
        self.assertEqual((doc["doc_id"], doc["doc_type"], doc["references_invoice"]),
                         ("SAGAR-CN-T", "credit_note", "SAGAR-T"))
        self.assertEqual(lines[0]["billed_total"], Decimal("-156.00"))

    def test_total_row_required(self):
        with self.assertRaises(ParseError):
            _run(sagar_csv, "S.csv", SAGAR_INVOICE.replace("TOTAL,,,,,,2250.00\n", ""))


class DetectTest(unittest.TestCase):
    def test_unknown(self):
        self.assertIsNone(detect(Path("x.pdf"), "%PDF-1.4"))
        self.assertIsNone(detect(Path("x.csv"), "a,b,c\n1,2,3\n"))
        self.assertIsNone(detect(Path("x.json"), '{"invoice_no": "X", "lines": []}'))


if __name__ == "__main__":
    unittest.main()
