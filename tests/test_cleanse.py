"""Tests for record cleaning, deduplication, quarantine routing and counts."""
from datetime import date

import pytest

from pipeline.cleanse import (apply_referential_checks, clean_customer, clean_product,
                              clean_transaction, process_entity)
from pipeline.config import BATCH_DATES, BATCH_FILES, get_settings, raw_path
from pipeline.readers import read_source

BATCH_DATE = date(2026, 9, 30)


def txn(**overrides):
    row = {"transaction_id": "T1", "account_id": "A1", "product_id": "P1", "branch_id": "B1",
           "date": "2026-09-01", "timestamp": "2026-09-01 10:00:00", "amount": "1000",
           "payment_method": "UPI", "status": "Success", "currency": "INR", "remarks": "", "is_refund": "False"}
    return {**row, **overrides}


def customer(**overrides):
    row = {"customer_id": "C1", "customer_name": "Customer_1", "email": "c1@example.com",
           "phone": "+91-9999999999", "account_id": "A1", "gender": "Male", "dob": "1990-01-01",
           "address": "1 Main St, Pune", "kyc_status": "Verified", "registration_date": "2020-01-01",
           "transaction_id": "T1"}
    return {**row, **overrides}


# ---------- single transactions ----------

def test_valid_transaction_passes():
    record, rejects, flags = clean_transaction(txn(), BATCH_DATE)
    assert rejects == [] and flags == []
    assert record["amount_inr"] == 1000 and record["status"] == "Success" and record["is_refund"] == 0


@pytest.mark.parametrize("overrides, reason", [
    ({"amount": ""}, "MISSING_AMOUNT"),
    ({"amount": "0"}, "ZERO_AMOUNT"),
    ({"amount": "-500"}, "NEGATIVE_AMOUNT"),
    ({"amount": "abc"}, "INVALID_AMOUNT"),
    ({"timestamp": "2026-10-05 10:00:00", "date": "2026-10-05"}, "FUTURE_DATED"),
    ({"status": "Done"}, "INVALID_STATUS"),
    ({"account_id": ""}, "MISSING_KEY"),
])
def test_bad_transactions_rejected_with_reason(overrides, reason):
    record, rejects, _ = clean_transaction(txn(**overrides), BATCH_DATE)
    assert record is None and reason in rejects


def test_all_reasons_reported_not_just_first():
    _, rejects, _ = clean_transaction(txn(amount="", status="???"), BATCH_DATE)
    assert {"MISSING_AMOUNT", "INVALID_STATUS"} <= set(rejects)


def test_date_mismatch_is_flagged_and_timestamp_wins():
    record, rejects, flags = clean_transaction(txn(date="2027-01-14"), BATCH_DATE)
    assert rejects == [] and "DATE_MISMATCH" in flags
    assert record["txn_date"] == date(2026, 9, 1) and record["date_mismatch"] == 1


def test_month_first_date_is_not_a_mismatch():
    _, _, flags = clean_transaction(txn(date="09/01/2026"), BATCH_DATE)
    assert "DATE_MISMATCH" not in flags


def test_non_inr_kept_but_excluded_from_rupee_value():
    record, rejects, flags = clean_transaction(txn(currency="eur"), BATCH_DATE)
    assert rejects == [] and "NON_INR_CURRENCY" in flags
    assert record["currency"] == "EUR" and record["amount_inr"] is None and record["needs_review"] == 1


def test_blank_refund_flag_defaults_to_false_and_is_flagged():
    record, _, flags = clean_transaction(txn(is_refund=""), BATCH_DATE)
    assert record["is_refund"] == 0 and "MISSING_REFUND_FLAG" in flags


def test_late_correction_marker_removed_from_remarks():
    record, _, _ = clean_transaction(txn(remarks="Bill Payment [late correction]"), BATCH_DATE)
    assert record["remarks"] == "Bill Payment"


# ---------- customers and products ----------

def test_customer_standardised():
    record, rejects, _ = clean_customer(customer(kyc_status=" verified", gender="F",
                                                 email=" C1@EXAMPLE.COM ", address="  1 main st, pune  "), BATCH_DATE)
    assert rejects == []
    assert (record["kyc_status"], record["gender"], record["email"], record["address"]) == \
           ("Verified", "Female", "c1@example.com", "1 Main St, Pune")


def test_future_dob_flagged_and_cleared():
    record, rejects, flags = clean_customer(customer(dob="2028-12-25"), BATCH_DATE)
    assert rejects == [] and "INVALID_DOB" in flags and record["dob"] is None


def test_customer_without_account_rejected():
    record, rejects, _ = clean_customer(customer(account_id=""), BATCH_DATE)
    assert record is None and "MISSING_ACCOUNT_ID" in rejects


def test_negative_price_corrected_and_flagged():
    raw = {"product_id": "P299", "product_name": "Product_2", "product_type": "Fixed Deposit",
           "price": -31593, "launch_date": "2018-09-23", "is_active": "TRUE", "category": "RETAIL"}
    record, rejects, flags = clean_product(raw, BATCH_DATE)
    assert rejects == [] and "NEGATIVE_PRICE_CORRECTED" in flags
    assert record["price"] == 31593 and record["is_active"] == 1 and record["category"] == "Retail"


# ---------- whole-file processing ----------

def balanced(result):
    return result.received == result.passed + len(result.quarantine) + result.duplicates


def test_exact_duplicate_transaction_kept_once():
    result = process_entity("transactions", "t.csv", [txn(), txn()], [], BATCH_DATE)
    assert (result.passed, result.duplicates, len(result.quarantine)) == (1, 1, 0) and balanced(result)


def test_conflicting_duplicate_transactions_all_quarantined():
    rows = [txn(amount="47753"), txn(amount="26116"), txn(amount="47753")]   # like T9125 in Day 2
    result = process_entity("transactions", "t.csv", rows, [], BATCH_DATE)
    assert result.passed == 0 and result.duplicates == 1 and len(result.quarantine) == 2
    assert all(q["reason_codes"] == "CONFLICTING_DUPLICATE" for q in result.quarantine)
    assert balanced(result)


def test_near_duplicate_customer_resolved_to_one():
    rows = [customer(customer_name="Customer_1  ", email="C1@EXAMPLE.COM"), customer()]
    result = process_entity("customers", "c.csv", rows, [], BATCH_DATE)
    assert result.passed == 1 and result.duplicates == 1 and balanced(result)


def test_best_quality_copy_wins():
    rows = [customer(phone="+91-123"), customer()]          # first copy has a bad phone
    result = process_entity("customers", "c.csv", rows, [], BATCH_DATE)
    assert result.clean[0]["phone_valid"] == 1


def test_shared_account_goes_to_later_customer_quarantine():
    rows = [customer(customer_id="C046", registration_date="2022-05-07"),
            customer(customer_id="C8252", registration_date="2025-08-03")]
    result = process_entity("customers", "c.csv", rows, [], BATCH_DATE)
    assert [c["customer_id"] for c in result.clean] == ["C046"]
    assert result.quarantine[0]["reason_codes"] == "SHARED_ACCOUNT_ID" and balanced(result)


def test_lost_json_fragment_is_quarantined():
    result = process_entity("products", "p.json", [], ['{"product_id": "P9", "pri'], BATCH_DATE)
    assert result.received == 1 and result.quarantine[0]["reason_codes"] == "MALFORMED_JSON"


def test_orphan_transaction_quarantined_with_original_record():
    result = process_entity("transactions", "t.csv", [txn(account_id="A_UNKNOWN")], [], BATCH_DATE)
    apply_referential_checks(result, accounts={"A1"}, products={"P1"}, branches={"B1"})
    assert result.passed == 0 and result.quarantine[0]["reason_codes"] == "ORPHAN_ACCOUNT"
    assert '"A_UNKNOWN"' in result.quarantine[0]["raw_record"] and balanced(result)


@pytest.mark.parametrize("batch_id", sorted(BATCH_FILES))
def test_every_real_file_reconciles(batch_id):
    """received = passed + rejected + duplicates for every real source file."""
    settings = get_settings()
    for entity, file_name in BATCH_FILES[batch_id].items():
        rows, _, lost = read_source(raw_path(settings, batch_id, entity))
        result = process_entity(entity, file_name, rows, lost, date.fromisoformat(BATCH_DATES[batch_id]))
        assert balanced(result), file_name
