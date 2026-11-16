"""Exact money and quantity parsing. Money is held as integer cents; rates and
quantities as Decimal. Ambiguous formats return None so callers can route to review."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# US-style amounts: optional $ and sign, optional thousands commas, optional decimals.
_MONEY_RE = re.compile(
    r"^(?P<neg>-|\()?\s*(?P<cur>US\$|\$|USD)?\s*(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?\)?$"
)
_QTY_RE = re.compile(r"^(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?$")


def parse_decimal_amount(text: str) -> Decimal | None:
    """Parse a printed amount like "$6,000.00" into a Decimal. Returns None for
    anything that is not unambiguously US formatted (e.g. "6.000,00")."""
    s = text.strip().replace("−", "-")
    m = _MONEY_RE.match(s)
    if not m:
        return None
    num = m.group("num").replace(",", "")
    dec = m.group("dec")
    # A lone comma group with 3 digits after a dot ("1.234") is ambiguous as a money value.
    if dec is not None and len(dec) == 3 and "," not in m.group("num") and m.group("cur") is None:
        return None
    try:
        value = Decimal(num + ("." + dec if dec else ""))
    except InvalidOperation:
        return None
    if m.group("neg"):
        value = -value
    return value


def parse_money_cents(text: str) -> int | None:
    """Parse a posted money amount to integer cents. Requires 0 or 2 decimals."""
    value = parse_decimal_amount(text)
    if value is None:
        return None
    if value != value.quantize(Decimal("0.01")):
        return None
    return int((value * 100).to_integral_value())


def parse_quantity(text: str) -> Decimal | None:
    m = _QTY_RE.match(text.strip())
    if not m:
        return None
    return Decimal(m.group("num").replace(",", "") + ("." + m.group("dec") if m.group("dec") else ""))


def decimal_to_cents(value: Decimal) -> int:
    """Round half-up to cents and return an int."""
    return int((value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100).to_integral_value())


def csv_money_cents(text: str) -> int:
    value = Decimal(text.strip())
    if value != value.quantize(Decimal("0.01")):
        raise ValueError(f"amount {text!r} has more than 2 decimals")
    return int((value * 100).to_integral_value())


def fmt_cents(cents: int | None, currency: str = "USD") -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{currency} {cents // 100:,}.{cents % 100:02d}"


def norm_decimal(value: Decimal) -> str:
    """Canonical string for storage (no exponent, trailing zeros trimmed)."""
    s = format(value.normalize(), "f")
    return s
