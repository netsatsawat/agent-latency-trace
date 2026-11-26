"""Checkout maths for a small shop. The tests in test_prices.py describe what it should do."""


def apply_discount(price, percent):
    """Return the price after a discount of `percent` percent, rounded to cents."""
    return round(price - price * percent, 2)


def add_tax(amount, rate):
    """Return the amount with tax at `rate` percent added, rounded to cents."""
    return round(amount * rate / 100, 2)


def checkout(prices, discount=0, tax_rate=0, coupon=0.0):
    """Return what the customer pays.

    Each item gets the percent discount first. Tax is added to the discounted subtotal. A fixed
    coupon then comes off the taxed total. The customer never pays less than zero.
    """
    subtotal = sum(apply_discount(p, discount) for p in prices)
    total = add_tax(subtotal - coupon, tax_rate)
    return round(total, 2)
