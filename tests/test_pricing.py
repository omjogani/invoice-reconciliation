import unittest
from decimal import Decimal

from recon.pricing import price
from tests.helpers import fixture_card


def ship(carrier: str, kg, km=100, service="standard", handling=()):
    return {"carrier": carrier, "billed_weight_kg": kg, "distance_km": km,
            "service_level": service, "special_handling": list(handling)}


class FalconPricingTest(unittest.TestCase):
    card = fixture_card("falcon")

    def test_express_worked_example(self):
        r = price(ship("falcon", 260, 800, "express"), self.card)
        self.assertEqual(r["expected"], Decimal("18547.20"))  # 800 x 18 x 1.15 x 1.12
        self.assertEqual([s["label"] for s in r["surcharges"]], ["express premium", "fuel surcharge"])
        self.assertEqual(r["surcharges"][1]["base"], Decimal("16560"))  # fuel on base + express
        self.assertEqual(r["clauses"], ["§1", "§2", "§3"])

    def test_band_edges(self):
        self.assertEqual(price(ship("falcon", 500, 100), self.card)["components"][0]["rate"], Decimal("18.00"))
        self.assertEqual(price(ship("falcon", 501, 100), self.card)["components"][0]["rate"], Decimal("24.00"))
        self.assertEqual(price(ship("falcon", 2000, 100), self.card)["components"][0]["rate"], Decimal("24.00"))

    def test_fractional_weight_between_bands_is_a_gap(self):
        r = price(ship("falcon", 500.5, 100), self.card)
        self.assertEqual((r["status"], r["code"]), ("undetermined", "CONTRACT_GAP"))
        self.assertEqual([c["amount"] for c in r["candidates"]], [Decimal("2016.00"), Decimal("2688.00")])

    def test_above_card_has_no_candidates(self):
        r = price(ship("falcon", 2500, 100), self.card)
        self.assertEqual((r["status"], r["code"], r["candidates"]), ("undetermined", "OUT_OF_CARD", []))

    def test_residential_accessorial(self):
        r = price(ship("falcon", 1800, 800, handling=["residential"]), self.card)
        self.assertEqual(r["expected"], Decimal("21754.00"))  # 800 x 24 x 1.12 + 250
        self.assertEqual(r["accessorials"][0]["amount"], Decimal("250.00"))


class AlpinePricingTest(unittest.TestCase):
    card = fixture_card("alpine")

    def test_minimum_chargeable_weight(self):
        r = price(ship("alpine", 18), self.card)
        self.assertEqual(r["weights"]["chargeable"], Decimal("25"))
        self.assertEqual(r["expected"], Decimal("237.50"))
        self.assertIn("§1", r["clauses"])

    def test_rounding_half_up(self):
        self.assertEqual(price(ship("alpine", 71.5), self.card)["expected"], Decimal("589.88"))

    def test_exactly_50_kg_is_a_gap_with_both_candidates(self):
        r = price(ship("alpine", 50.0), self.card)
        self.assertEqual((r["status"], r["code"], r["expected"]), ("undetermined", "CONTRACT_GAP", None))
        self.assertEqual([c["amount"] for c in r["candidates"]], [Decimal("475.00"), Decimal("412.50")])
        self.assertIn("between the bands", r["reason"])

    def test_fragile_handling(self):
        self.assertEqual(price(ship("alpine", 60, handling=["fragile"]), self.card)["expected"], Decimal("645.00"))

    def test_service_not_offered_is_flagged(self):
        self.assertEqual(price(ship("alpine", 30, service="express"), self.card)["flags"], ["service_not_offered"])


class SagarPricingTest(unittest.TestCase):
    card = fixture_card("sagar")

    def test_weight_plus_distance(self):
        self.assertEqual(price(ship("sagar", 260, 60), self.card)["expected"], Decimal("1680.00"))

    def test_cold_chain_premium(self):
        r = price(ship("sagar", 200, 110, handling=["cold_chain"]), self.card)
        self.assertEqual(r["freight"], Decimal("1420.00"))
        self.assertEqual(r["expected"], Decimal("1704.00"))


if __name__ == "__main__":
    unittest.main()
