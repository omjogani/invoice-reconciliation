import unittest
from decimal import Decimal

from recon.money import D, amount_spellings, fmt_indian, fmt_western, parse_amount, q2, to_number, to_str


class ParseAmountTest(unittest.TestCase):
    def test_printed_forms(self):
        self.assertEqual(parse_amount("Rs 17,472.00"), Decimal("17472.00"))
        self.assertEqual(parse_amount("₹9.50"), Decimal("9.50"))
        self.assertEqual(parse_amount("-496.80"), Decimal("-496.80"))
        self.assertEqual(parse_amount("Rs -496.80"), Decimal("-496.80"))
        self.assertEqual(parse_amount("-Rs 496.80"), Decimal("-496.80"))
        self.assertEqual(parse_amount(" 1,81,637.60 "), Decimal("181637.60"))

    def test_rejects_garbage(self):
        for bad in ("", "Rs", "abc", "12..3", "NaN", "Infinity"):
            with self.assertRaises(ValueError, msg=bad):
                parse_amount(bad)

    def test_D_from_json_float_is_exact(self):
        self.assertEqual(D(589.88), Decimal("589.88"))
        self.assertEqual(D(25), Decimal("25"))
        with self.assertRaises(TypeError):
            D(True)


class RoundingTest(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(q2(Decimal("589.875")), Decimal("589.88"))
        self.assertEqual(q2(Decimal("878.8755")), Decimal("878.88"))
        self.assertEqual(q2(Decimal("1028.0875")), Decimal("1028.09"))
        self.assertEqual(q2(Decimal("-0.005")), Decimal("-0.01"))

    def test_serialisation(self):
        self.assertEqual(to_str(Decimal("18547.2")), "18547.20")
        self.assertEqual(to_number(Decimal("18547.20")), 18547.2)
        self.assertIsNone(to_number(None))


class FormattingTest(unittest.TestCase):
    def test_groupings(self):
        self.assertEqual(fmt_western(Decimal("1234567.5")), "1,234,567.50")
        self.assertEqual(fmt_indian(Decimal("1234567.5")), "12,34,567.50")
        self.assertEqual(fmt_indian(Decimal("999")), "999.00")
        self.assertEqual(fmt_indian(Decimal("-7392")), "-7,392.00")

    def test_spellings(self):
        self.assertEqual(amount_spellings(Decimal("-1200")), {"1200.00", "1,200.00"})
        self.assertIn("1,98,135.20", amount_spellings(Decimal("198135.2")))


if __name__ == "__main__":
    unittest.main()
