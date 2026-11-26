import unittest

from prices import add_tax, apply_discount, checkout


class TestPrices(unittest.TestCase):
    def test_discount(self):
        self.assertEqual(apply_discount(80.0, 25), 60.0)

    def test_no_discount(self):
        self.assertEqual(apply_discount(19.99, 0), 19.99)

    def test_tax(self):
        self.assertEqual(add_tax(100.0, 7), 107.0)

    def test_checkout_plain(self):
        self.assertEqual(checkout([10.0, 30.0]), 40.0)

    def test_checkout_discount_and_tax(self):
        self.assertEqual(checkout([10.0, 30.0], discount=10, tax_rate=7), 38.52)

    def test_checkout_coupon_after_tax(self):
        self.assertEqual(checkout([50.0], tax_rate=10, coupon=5.0), 50.0)

    def test_checkout_never_negative(self):
        self.assertEqual(checkout([3.0], coupon=5.0), 0.0)


if __name__ == "__main__":
    unittest.main()
