import unittest
from datetime import date

from billing import add_months, next_billing_date


class TestBilling(unittest.TestCase):
    def test_same_day_next_month(self):
        self.assertEqual(add_months(date(2026, 3, 15), 1), date(2026, 4, 15))

    def test_month_end_clamps(self):
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))

    def test_into_december(self):
        self.assertEqual(add_months(date(2025, 11, 30), 1), date(2025, 12, 30))

    def test_across_new_year(self):
        self.assertEqual(add_months(date(2025, 11, 30), 3), date(2026, 2, 28))

    def test_next_billing_simple(self):
        self.assertEqual(next_billing_date(date(2026, 1, 10), date(2026, 3, 1)), date(2026, 3, 10))

    def test_next_billing_does_not_drift(self):
        self.assertEqual(next_billing_date(date(2024, 1, 31), date(2024, 3, 1)), date(2024, 3, 31))

    def test_billing_day_is_not_today(self):
        self.assertEqual(next_billing_date(date(2026, 1, 10), date(2026, 2, 10)), date(2026, 3, 10))


if __name__ == "__main__":
    unittest.main()
