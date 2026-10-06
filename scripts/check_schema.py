"""Phase 4 check: apply the schema, then prove the constraints work.

Every test row is written inside one transaction that is rolled back at the
end, so the database is left exactly as it was.
Usage:  python -m scripts.check_schema
"""
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from pipeline.db import apply_schema, get_engine

EXPECTED_TABLES = {
    "dim_customer", "dim_product", "dim_branch", "fact_transaction",
    "fact_correction_log", "ingestion_log", "pipeline_runs", "dq_quarantine",
    "dq_scorecard", "dq_issue_counts", "kpi_snapshot",
}
H = "0" * 64  # dummy row_hash

CUSTOMER = ("INSERT INTO dim_customer (customer_id, customer_name, email_valid, phone_valid, account_id, "
            "gender, kyc_status, valid_from, valid_to, is_current, row_hash, batch_id) "
            "VALUES (:id, 'Test', 1, 1, 'ATEST', 'Male', :kyc, :vf, :vt, :cur, :h, 'test')")
PRODUCT = ("INSERT INTO dim_product (product_id, product_type, category, price, is_active, valid_from, row_hash, batch_id) "
           "VALUES ('PTEST', 'Loan', 'Retail', :price, 1, '1900-01-01', :h, 'test')")
BRANCH = ("INSERT INTO dim_branch (branch_id, branch_name, location, manager_name, region, branch_type, valid_from, row_hash, batch_id) "
          "VALUES ('BTEST', 'Test', 'Pune', 'M', 'West', 'Urban', '1900-01-01', :h, 'test')")
FACT = ("INSERT INTO fact_transaction (transaction_id, account_id, customer_sk, product_sk, branch_sk, customer_id, "
        "product_id, branch_id, txn_ts, txn_date, amount, currency, amount_inr, payment_method, status, is_refund, "
        "source_batch_id, last_updated_batch_id, row_hash) VALUES ('TTEST', 'ATEST', :c, :p, :b, 'CTEST', 'PTEST', "
        "'BTEST', '2026-09-01 10:00:00', '2026-09-01', :amt, 'INR', :amt, 'UPI', 'Success', 0, 'test', 'test', :h)")


def expect_rejected(conn, label, sql, params):
    savepoint = conn.begin_nested()
    try:
        conn.execute(text(sql), params)
    except (IntegrityError, OperationalError) as exc:
        savepoint.rollback()
        print(f"PASS  {label}: rejected ({exc.orig.args[1][:70]})")
        return True
    savepoint.rollback()
    print(f"FAIL  {label}: was accepted")
    return False


def main():
    engine = get_engine()
    print(f"schema applied: {apply_schema(engine)} statements")
    tables = set(inspect(engine).get_table_names())
    missing = EXPECTED_TABLES - tables
    print(f"{'PASS' if not missing else 'FAIL'}  tables present: {len(EXPECTED_TABLES & tables)}/{len(EXPECTED_TABLES)}"
          + (f" missing {missing}" if missing else ""))
    results = [not missing]

    with engine.connect() as conn:
        tx = conn.begin()
        # Valid rows go in: one version of each dimension + one fact.
        conn.execute(text(CUSTOMER), {"id": "CTEST", "kyc": "Pending", "vf": "1900-01-01", "vt": "9999-12-31", "cur": 1, "h": H})
        conn.execute(text(PRODUCT), {"price": 100, "h": H})
        conn.execute(text(BRANCH), {"h": H})
        sk = lambda t, col, key: conn.execute(text(f"SELECT {col}_sk FROM {t} WHERE {col}_id = :k"), {"k": key}).scalar()
        c, p, b = sk("dim_customer", "customer", "CTEST"), sk("dim_product", "product", "PTEST"), sk("dim_branch", "branch", "BTEST")
        conn.execute(text(FACT), {"c": c, "p": p, "b": b, "amt": 500, "h": H})
        joined = conn.execute(text(
            "SELECT f.transaction_id, cu.kyc_status, pr.product_type, br.region FROM fact_transaction f "
            "JOIN dim_customer cu ON cu.customer_sk = f.customer_sk JOIN dim_product pr ON pr.product_sk = f.product_sk "
            "JOIN dim_branch br ON br.branch_sk = f.branch_sk WHERE f.transaction_id = 'TTEST'")).fetchone()
        print(f"{'PASS' if joined else 'FAIL'}  valid rows insert and star-join: {tuple(joined) if joined else None}")
        results.append(bool(joined))

        # Each constraint must block bad data.
        results.append(expect_rejected(conn, "duplicate transaction_id (fact grain)", FACT,
                                       {"c": c, "p": p, "b": b, "amt": 500, "h": H}))
        results.append(expect_rejected(conn, "second CURRENT version of same customer", CUSTOMER,
                                       {"id": "CTEST", "kyc": "Verified", "vf": "2026-10-01", "vt": "9999-12-31", "cur": 1, "h": H}))
        results.append(expect_rejected(conn, "fact pointing at a missing customer (FK)",
                                       FACT.replace("'TTEST'", "'TTEST2'", 1), {"c": 999999, "p": p, "b": b, "amt": 500, "h": H}))
        results.append(expect_rejected(conn, "negative product price (CHECK)",
                                       PRODUCT.replace("'PTEST'", "'PTEST2'"), {"price": -5, "h": H}))
        results.append(expect_rejected(conn, "zero / negative amount (CHECK)",
                                       FACT.replace("'TTEST'", "'TTEST3'", 1), {"c": c, "p": p, "b": b, "amt": 0, "h": H}))
        results.append(expect_rejected(conn, "scorecard that doesn't balance (CHECK)",
                                       "INSERT INTO dq_scorecard VALUES ('test','f.csv','x',10,5,2,1,'r')", {}))

        # SCD2: closing the old version then adding a new current one IS allowed.
        conn.execute(text("UPDATE dim_customer SET is_current = 0, valid_to = '2026-10-01' WHERE customer_id = 'CTEST'"))
        conn.execute(text(CUSTOMER), {"id": "CTEST", "kyc": "Verified", "vf": "2026-10-01", "vt": "9999-12-31", "cur": 1, "h": H})
        versions = conn.execute(text("SELECT kyc_status, is_current FROM dim_customer WHERE customer_id='CTEST' ORDER BY valid_from")).fetchall()
        ok = [tuple(v) for v in versions] == [("Pending", 0), ("Verified", 1)]
        print(f"{'PASS' if ok else 'FAIL'}  SCD2 close-and-insert keeps history: {[tuple(v) for v in versions]}")
        results.append(ok)
        tx.rollback()

    left = engine.connect().execute(text("SELECT COUNT(*) FROM dim_customer WHERE customer_id='CTEST'")).scalar()
    print(f"{'PASS' if left == 0 else 'FAIL'}  test rows rolled back (left: {left})")
    results.append(left == 0)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
