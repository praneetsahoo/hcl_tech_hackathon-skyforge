"""Tests for the SCD2 merge and the fact upsert, on an in-memory SQLite
database with the same columns as MySQL (no AWS needed)."""
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from pipeline.load import load_facts, merge_dimension

sqlite3.register_adapter(date, date.isoformat)
sqlite3.register_adapter(datetime, lambda d: d.isoformat(sep=" "))
sqlite3.register_adapter(Decimal, str)
sqlite3.register_converter("DATE", lambda b: date.fromisoformat(b.decode()))
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))

SCD = "valid_from DATE, valid_to DATE, is_current INTEGER, row_hash TEXT, batch_id TEXT"
DDL = [
    f"""CREATE TABLE dim_customer (customer_sk INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT,
        customer_name TEXT, email TEXT, email_valid INTEGER, phone TEXT, phone_valid INTEGER, account_id TEXT,
        gender TEXT, dob DATE, address TEXT, kyc_status TEXT, registration_date DATE, {SCD})""",
    f"""CREATE TABLE dim_product (product_sk INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, product_name TEXT,
        product_type TEXT, category TEXT, price TEXT, launch_date DATE, is_active INTEGER, vendor_name TEXT, {SCD})""",
    f"""CREATE TABLE dim_branch (branch_sk INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT, branch_name TEXT,
        location TEXT, manager_name TEXT, opened_date DATE, region TEXT, branch_type TEXT, contact_number TEXT, {SCD})""",
    """CREATE TABLE fact_transaction (transaction_id TEXT PRIMARY KEY, account_id TEXT, customer_sk INTEGER,
        product_sk INTEGER, branch_sk INTEGER, customer_id TEXT, product_id TEXT, branch_id TEXT, txn_ts TIMESTAMP,
        txn_date DATE, amount TEXT, currency TEXT, amount_inr TEXT, payment_method TEXT, status TEXT,
        is_refund INTEGER, remarks TEXT, date_mismatch INTEGER, needs_review INTEGER, source_batch_id TEXT,
        last_updated_batch_id TEXT, row_hash TEXT)""",
    """CREATE TABLE fact_correction_log (correction_id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT,
        batch_id TEXT, run_id TEXT, old_values TEXT, new_values TEXT, corrected_at TEXT)""",
]
DAY1, DAY2 = date(2026, 9, 30), date(2026, 10, 1)


@pytest.fixture
def conn():
    engine = create_engine("sqlite://", connect_args={"detect_types": sqlite3.PARSE_DECLTYPES})
    with engine.begin() as c:
        for ddl in DDL:
            c.execute(text(ddl))
        yield c


def cust(**kw):
    base = {"customer_id": "C1", "customer_name": "Customer_1", "email": "c1@x.com", "email_valid": 1,
            "phone": "+91-9999999999", "phone_valid": 1, "account_id": "A1", "gender": "Male",
            "dob": date(1990, 1, 1), "address": "1 Main St", "kyc_status": "Pending",
            "registration_date": date(2020, 1, 1)}
    return {**base, **kw}


def versions(conn):
    return [tuple(r) for r in conn.execute(text(
        "SELECT kyc_status, valid_from, valid_to, is_current FROM dim_customer ORDER BY valid_from"))]


# ---------- SCD Type 2 ----------

def test_first_load_inserts_and_rerun_is_unchanged(conn):
    assert merge_dimension(conn, "customers", [cust()], "day1", DAY1)["inserted"] == 1
    again = merge_dimension(conn, "customers", [cust()], "day1", DAY1)
    assert again == {"inserted": 0, "new_version": 0, "updated_in_place": 0, "unchanged": 1}


def test_kyc_change_creates_new_version_and_keeps_history(conn):
    merge_dimension(conn, "customers", [cust()], "day1", DAY1)
    counts = merge_dimension(conn, "customers", [cust(kyc_status="Verified")], "day2", DAY2)
    assert counts["new_version"] == 1
    assert versions(conn) == [("Pending", date(1900, 1, 1), DAY2, 0),
                              ("Verified", DAY2, date(9999, 12, 31), 1)]


def test_rerunning_day2_does_not_add_another_version(conn):
    merge_dimension(conn, "customers", [cust()], "day1", DAY1)
    merge_dimension(conn, "customers", [cust(kyc_status="Verified")], "day2", DAY2)
    assert merge_dimension(conn, "customers", [cust(kyc_status="Verified")], "day2", DAY2)["unchanged"] == 1
    assert len(versions(conn)) == 2


def test_minor_change_is_updated_in_place_without_history(conn):
    merge_dimension(conn, "customers", [cust()], "day1", DAY1)
    counts = merge_dimension(conn, "customers", [cust(customer_name="Customer One")], "day2", DAY2)
    assert counts["updated_in_place"] == 1 and len(versions(conn)) == 1


# ---------- fact upsert ----------

def setup_dims(conn):
    merge_dimension(conn, "customers", [cust()], "day1", DAY1)
    merge_dimension(conn, "products", [{"product_id": "P1", "product_name": "Loan 1", "product_type": "Loan",
                                        "category": "Retail", "price": Decimal("100.00"), "launch_date": None,
                                        "is_active": 1, "vendor_name": "V"}], "day1", DAY1)
    merge_dimension(conn, "branches", [{"branch_id": "B1", "branch_name": "B", "location": "Pune",
                                        "manager_name": "M", "opened_date": None, "region": "West",
                                        "branch_type": "Urban", "contact_number": None}], "day1", DAY1)


def fact(**kw):
    ts = kw.pop("ts", datetime(2026, 9, 1, 10, 0))
    base = {"transaction_id": "T1", "account_id": "A1", "product_id": "P1", "branch_id": "B1",
            "txn_ts": ts, "txn_date": ts.date(), "amount": Decimal("1000.00"), "currency": "INR",
            "amount_inr": Decimal("1000.00"), "payment_method": "UPI", "status": "Failed", "is_refund": 0,
            "remarks": None, "date_mismatch": 0, "needs_review": 0}
    return {**base, **kw}


def count_facts(conn):
    return conn.execute(text("SELECT COUNT(*) FROM fact_transaction")).scalar()


def test_fact_insert_then_rerun_unchanged(conn):
    setup_dims(conn)
    assert load_facts(conn, [fact()], "day1", "run1")["inserted"] == 1
    assert load_facts(conn, [fact()], "day1", "run2") == {"inserted": 0, "corrected": 0, "unchanged": 1}
    assert count_facts(conn) == 1


def test_late_correction_updates_same_row_and_is_logged(conn):
    setup_dims(conn)
    load_facts(conn, [fact()], "day1", "run1")
    counts = load_facts(conn, [fact(status="Success")], "day2", "run2")
    assert counts["corrected"] == 1 and count_facts(conn) == 1            # no double counting
    row = conn.execute(text("SELECT status, source_batch_id, last_updated_batch_id FROM fact_transaction")).one()
    assert tuple(row) == ("Success", "day1", "day2")
    log = conn.execute(text("SELECT old_values, new_values FROM fact_correction_log")).one()
    assert json.loads(log[0]) == {"status": "Failed"} and json.loads(log[1]) == {"status": "Success"}


def test_fact_links_to_customer_version_valid_at_transaction_time(conn):
    setup_dims(conn)
    merge_dimension(conn, "customers", [cust(kyc_status="Verified")], "day2", DAY2)
    load_facts(conn, [fact(transaction_id="T_OLD", ts=datetime(2026, 9, 1, 10, 0)),
                      fact(transaction_id="T_NEW", ts=datetime(2026, 10, 1, 10, 0))], "day2", "run1")
    rows = dict(conn.execute(text(
        "SELECT f.transaction_id, c.kyc_status FROM fact_transaction f "
        "JOIN dim_customer c ON c.customer_sk = f.customer_sk")).fetchall())
    assert rows == {"T_OLD": "Pending", "T_NEW": "Verified"}
