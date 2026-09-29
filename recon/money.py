"""Money handling: exact decimals, half-up rounding to the paisa.

Binary floats never touch an amount. Values enter as strings (from the
source documents or our own JSON files) and are converted to Decimal here.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISA = Decimal("0.01")
ZERO = Decimal("0")

_CURRENCY_NOISE = re.compile(r"(?i)^\s*(rs\.?|inr|₹)\s*")


def D(value: object) -> Decimal:
    """Convert a number or numeric string to an exact Decimal.

    Floats are converted through ``repr`` so that a JSON value such as
    ``589.88`` becomes ``Decimal("589.88")`` and not its binary expansion.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("booleans are not amounts")
    if isinstance(value, (int, float)):
        return Decimal(repr(value))
    if isinstance(value, str):
        return parse_amount(value)
    raise TypeError(f"cannot convert {type(value).__name__} to an amount")


def parse_amount(text: str) -> Decimal:
    """Parse a printed amount such as ``"Rs 17,472.00"``, ``"-496.80"`` or ``"₹9.50"``."""
    cleaned = _CURRENCY_NOISE.sub("", text.strip()).replace(",", "").replace(" ", "")
    if not cleaned:
        raise ValueError(f"empty amount: {text!r}")
    # A currency marker can also follow a sign: "-Rs 496.80".
    if cleaned[0] in "+-":
        cleaned = cleaned[0] + _CURRENCY_NOISE.sub("", cleaned[1:])
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    if not value.is_finite():
        raise ValueError(f"not a finite amount: {text!r}")
    return value


def q2(value: Decimal) -> Decimal:
    """Round half-up to the paisa."""
    return D(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def to_str(value: Decimal) -> str:
    """Serialise an amount for our own files: plain digits, two decimals."""
    return format(q2(value), "f")


def to_number(value: Decimal | None) -> float | None:
    """Convert a rounded amount to a JSON number for the final report.

    Only used at the very last step. A two-decimal Decimal converts to the
    float whose shortest repr is the same decimal text, so no precision is lost.
    """
    if value is None:
        return None
    return float(q2(value))


def fmt_western(value: Decimal) -> str:
    """``1234567.5`` → ``"1,234,567.50"``."""
    return format(q2(value), ",.2f")


def fmt_indian(value: Decimal) -> str:
    """``1234567.5`` → ``"12,34,567.50"`` (lakh grouping)."""
    v = q2(value)
    sign = "-" if v < 0 else ""
    whole, frac = format(abs(v), "f").split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


def amount_spellings(value: Decimal) -> set[str]:
    """Every accepted way a memo may print an amount (sign dropped)."""
    v = abs(q2(value))
    return {format(v, "f"), fmt_western(v), fmt_indian(v)}
