"""Unit tests for the single-value cleaning rules."""
from datetime import date
from decimal import Decimal

import pytest

from pipeline import rules


@pytest.mark.parametrize("raw, expected", [
    ("81250", Decimal("81250.00")),
    ("Rs.81250", Decimal("81250.00")),      # currency prefix
    ("₹1,200", Decimal("1200.00")),
    ("163,970", Decimal("163970.00")),      # comma-formatted
    (" 500 ", Decimal("500.00")),
    ("-112836", Decimal("-112836.00")),     # parsed; the transaction rule rejects it
    ("", None),
    ("abc", None),
])
def test_parse_amount(raw, expected):
    assert rules.parse_amount(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Success", "Success"), ("SUCCESS", "Success"), ("Succes", "Success"), ("sucess", "Success"),
    ("Faild", "Failed"), ("FAILED", "Failed"), ("pending", "Pending"), ("unknown", None),
])
def test_status_spellings(raw, expected):
    assert rules.lookup(raw, rules.STATUS) == expected


@pytest.mark.parametrize("raw, expected", [
    ("True", True), ("Y", True), ("1", True), ("False", False), ("N", False), ("0", False), ("", None),
])
def test_refund_flag_spellings(raw, expected):
    assert rules.lookup(raw, rules.REFUND) == expected


def test_lookup_ignores_case_and_spaces():
    assert rules.lookup(" west ", rules.REGION) == "West"
    assert rules.lookup("Card ", rules.PAYMENT) == "Card"
    assert rules.lookup("Corporate ", rules.CATEGORY) == "Corporate"


def test_ambiguous_date_has_two_readings_day_first():
    assert rules.date_candidates("01/10/2026") == [date(2026, 10, 1), date(2026, 1, 10)]
    parsed, ambiguous = rules.parse_date("01/10/2026")
    assert parsed == date(2026, 10, 1) and ambiguous


@pytest.mark.parametrize("raw, expected", [
    ("2026-09-30", date(2026, 9, 30)),
    ("2026/09/30", date(2026, 9, 30)),
    ("13-05-2021", date(2021, 5, 13)),     # only day-first is valid
    ("11/21/2004", date(2004, 11, 21)),    # only month-first is valid
    ("", None),
    ("not a date", None),
])
def test_parse_date_formats(raw, expected):
    assert rules.parse_date(raw)[0] == expected


@pytest.mark.parametrize("raw, expected", [
    ("customer_1@example.com", ("customer_1@example.com", True)),
    (" CUSTOMER_4@EXAMPLE.COM ", ("customer_4@example.com", True)),   # case/space fixed
    ("customer_99@@example.com", ("customer_99@@example.com", False)),
    ("customer_165example.com", ("customer_165example.com", False)),
    ("", (None, False)),
])
def test_clean_email(raw, expected):
    assert rules.clean_email(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("+91-9970629620", ("+91-9970629620", True)),
    ("+91 53950 24026", ("+91-5395024026", True)),
    ("9042284210", ("+91-9042284210", True)),
    ("910797647545", ("+91-0797647545", True)),
    ("+91-48175496", ("+91-48175496", False)),          # too short
])
def test_clean_phone(raw, expected):
    assert rules.clean_phone(raw) == expected


def test_parse_bool_variants():
    assert [rules.parse_bool(v) for v in (True, "true", "True", "FALSE", None)] == [True, True, True, False, None]
