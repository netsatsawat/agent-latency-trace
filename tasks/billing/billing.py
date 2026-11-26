"""Monthly billing dates for a subscription. The tests in test_billing.py describe what it should do."""
import calendar
from datetime import date


def add_months(d, months):
    """Return the same day `months` months later.

    If the target month is shorter, use its last day, so 31 January plus 1 month is the last day
    of February.
    """
    month = d.month + months
    year = d.year + month // 12
    month = month % 12
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_billing_date(start, today):
    """Return the first billing date after `today`.

    A subscription bills every month on the day it started, counted from `start`, so a plan that
    starts on the 31st bills on the last day of short months and on the 31st again after them.
    """
    billing = add_months(start, 1)
    while billing <= today:
        billing = add_months(billing, 1)
    return billing
