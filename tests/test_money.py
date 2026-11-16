from decimal import Decimal

from app.money import decimal_to_cents, parse_decimal_amount, parse_money_cents, parse_quantity


def test_money_parsing():
    assert parse_money_cents("$6,000.00") == 600000
    assert parse_money_cents("6000") == 600000
    assert parse_money_cents("$0.00") == 0
    assert parse_money_cents("USD 1,234.56") == 123456
    assert parse_money_cents("(12.50)") == -1250


def test_ambiguous_or_invalid_money_is_rejected():
    assert parse_money_cents("6.000,00") is None  # European separators -> review
    assert parse_money_cents("12.345") is None  # more than 2 decimals for a posted amount
    assert parse_money_cents("abc") is None
    assert parse_money_cents("1,23.00") is None


def test_rates_and_quantities():
    assert parse_decimal_amount("$12.345") == Decimal("12.345")
    assert parse_quantity("60") == Decimal("60")
    assert parse_quantity("1,250.5") == Decimal("1250.5")
    assert parse_quantity("6O") is None
    assert decimal_to_cents(Decimal("0.005")) == 1
