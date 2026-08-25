"""
Nexo POS - Money helpers

Non-functional requirement from the Phase 2+ audit: all monetary values are
stored and computed as integer cents, never float/REAL. This module is the
single place that converts between user-facing decimal strings/Decimal values
and the integer cents stored in the DB and used for all arithmetic.

Why integer cents instead of Decimal end-to-end: SQLite has no native Decimal
type, so anything stored as REAL round-trips through binary floating point
regardless of what Python type touches it in between. Integer cents avoids
that entirely — all storage and arithmetic (SUM, differences, comparisons)
stay exact, which is the whole point of the reconciliation formulas in
Feature 3 being expected to land on exactly 0.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class InvalidMoneyError(ValueError):
    pass


def parse_to_cents(value):
    """Parse a user-supplied decimal string (e.g. form input '19.99') into
    integer cents. Raises InvalidMoneyError on anything that isn't a clean
    non-negative decimal with at most 2 fractional digits worth of precision
    (extra digits are rounded half-up, same as cash rounding)."""
    if value is None or str(value).strip() == "":
        raise InvalidMoneyError("Amount is required")
    try:
        decimal_value = Decimal(str(value).strip())
    except InvalidOperation:
        raise InvalidMoneyError(f"Invalid amount: {value!r}")
    if decimal_value < 0:
        raise InvalidMoneyError("Amount cannot be negative")
    cents = (decimal_value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def cents_to_decimal(cents):
    """Integer cents -> Decimal with exactly 2 fractional digits, for display
    or for handing to templates."""
    return (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))


def format_cents(cents):
    """Integer cents -> '19.99' formatted string."""
    return f"{cents_to_decimal(cents):.2f}"
