"""Phase 6 check: KPI views give the KNOWN answers on a small hand-made fixture,
the 10-minute window agrees with an independent self-join, and every view
runs on the real data. Fixture rows are rolled back at the end.
Usage:  python -m scripts.check_kpis
"""
from sqlalchemy import text

from pipeline.db import apply_schema, get_engine

H = "0" * 64
VIEWS = ["kpi01_top_customers", "kpi02_branch_monthly", "kpi02_region_monthly", "kpi03_product_revenue",
         "kpi03_product_share", "kpi04_dormant_accounts", "kpi05_suspicious_transactions",
         "kpi06_customer_segments", "kpi07_branch_ranking", "kpi08_kyc_exposure", "kpi09_refund_by_product",
         "kpi09_refund_by_branch", "kpi10_dq_scorecard", "kpi11_incremental_counts", "kpi11_kpi_deltas",
         "kpi12_kyc_transitions", "kpi13_new_activations"]

# Fixture: (txn_id, account, branch, timestamp, amount, is_refund)
FIXTURE_TXNS = [
    ("TKA1", "AKA", "BKT1", "2026-09-10 10:00:00", 1000, 0),   # 1st of 3 in 10 min
    ("TKA2", "AKA", "BKT1", "2026-09-10 10:04:00", 1000, 0),   # 2nd
    ("TKA3", "AKA", "BKT1", "2026-09-10 10:09:00", 1000, 0),   # 3rd -> VELOCITY
    ("TKA4", "AKA", "BKT1", "2026-09-10 11:00:00", 400, 1),    # refund -> net 3000-400 = 2600
    ("TKB1", "AKB", "BKT2", "2026-09-11 10:00:00", 500, 0),    # only 2 in 10 min -> not flagged
    ("TKB2", "AKB", "BKT2", "2026-09-11 10:05:00", 500, 0),
    ("TKB3", "AKB", "BKT2", "2026-09-11 02:30:00", 700, 0),    # ODD_HOURS
    ("TKB4", "AKB", "BKT2", "2026-09-11 12:00:00", 150000, 0), # HIGH_VALUE
]


def insert_fixture(conn):
    for cid, acc, kyc in (("CKA", "AKA", "Pending"), ("CKB", "AKB", "Verified")):
        conn.execute(text("INSERT INTO dim_customer (customer_id, customer_name, email_valid, phone_valid, account_id, "
                          "gender, kyc_status, valid_from, row_hash, batch_id) VALUES (:c, :c, 1, 1, :a, 'Male', :k, "
                          "'1900-01-01', :h, 'test')"), {"c": cid, "a": acc, "k": kyc, "h": H})
    conn.execute(text("INSERT INTO dim_product (product_id, product_type, category, price, is_active, valid_from, "
                      "row_hash, batch_id) VALUES ('PKT', 'Loan', 'Retail', 1, 1, '1900-01-01', :h, 'test')"), {"h": H})
    for bid in ("BKT1", "BKT2"):
        conn.execute(text("INSERT INTO dim_branch (branch_id, branch_name, location, manager_name, region, branch_type, "
                          "valid_from, row_hash, batch_id) VALUES (:b, :b, 'X', 'M', 'Testland', 'Urban', '1900-01-01', "
                          ":h, 'test')"), {"b": bid, "h": H})
    sk = lambda table, col, key: conn.execute(text(f"SELECT {col}_sk FROM {table} WHERE {col}_id = :k"), {"k": key}).scalar()
    for tid, acc, bid, ts, amount, refund in FIXTURE_TXNS:
        cid = "CKA" if acc == "AKA" else "CKB"
        conn.execute(text(
            "INSERT INTO fact_transaction (transaction_id, account_id, customer_sk, product_sk, branch_sk, customer_id, "
            "product_id, branch_id, txn_ts, txn_date, amount, currency, amount_inr, payment_method, status, is_refund, "
            "source_batch_id, last_updated_batch_id, row_hash) VALUES (:t, :a, :c, :p, :b, :cid, 'PKT', :bid, :ts, "
            "DATE(:ts), :amt, 'INR', :amt, 'UPI', 'Success', :r, 'test', 'test', :h)"),
            {"t": tid, "a": acc, "c": sk("dim_customer", "customer", cid), "p": sk("dim_product", "product", "PKT"),
             "b": sk("dim_branch", "branch", bid), "cid": cid, "bid": bid, "ts": ts, "amt": amount, "r": refund, "h": H})
    # SCD2 change for KPI 12: CKA moves Pending -> Verified
    conn.execute(text("UPDATE dim_customer SET is_current = 0, valid_to = '2026-10-01' WHERE customer_id = 'CKA'"))
    conn.execute(text("INSERT INTO dim_customer (customer_id, customer_name, email_valid, phone_valid, account_id, gender, "
                      "kyc_status, valid_from, row_hash, batch_id) VALUES ('CKA', 'CKA', 1, 1, 'AKA', 'Male', "
                      "'Verified', '2026-10-01', :h, 'test')"), {"h": H})


def check(label, actual, expected, results):
    ok = actual == expected
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {actual!r}" + ("" if ok else f", expected {expected!r}"))


def main():
    engine = get_engine()
    print(f"schema + views applied: {apply_schema(engine)} statements")
    results = []
    with engine.connect() as conn:
        tx = conn.begin()
        insert_fixture(conn)
        one = lambda sql: conn.execute(text(sql)).fetchall()

        flagged = {r[0]: (r[1], r[2]) for r in one(
            "SELECT transaction_id, txns_in_10_min, risk_reason FROM kpi05_suspicious_transactions "
            "WHERE transaction_id IN ('TKA1','TKA2','TKA3','TKA4','TKB1','TKB2','TKB3','TKB4')")}
        check("KPI5 velocity: 3rd txn in 10 min flagged", flagged.get("TKA3"), (3, "VELOCITY_3_IN_10_MIN"), results)
        check("KPI5 velocity: 2 txns in 10 min NOT flagged", sorted(k for k in flagged if k in ("TKA1", "TKA2", "TKB1", "TKB2")), [], results)
        check("KPI5 odd hours (02:30)", flagged.get("TKB3", (None, None))[1], "ODD_HOURS", results)
        check("KPI5 high value (1,50,000)", flagged.get("TKB4", (None, None))[1], "HIGH_VALUE", results)
        check("KPI1 net value subtracts refunds (3000 - 400)",
              float(one("SELECT SUM(net_inr) FROM v_txn_value WHERE customer_id = 'CKA'")[0][0]), 2600.0, results)
        ranking = {r[0]: (r[1], r[2]) for r in one(
            "SELECT branch_id, rank_in_region, performer FROM kpi07_branch_ranking WHERE region = 'Testland'")}
        check("KPI7 rank within region", ranking, {"BKT2": (1, "Top performer"), "BKT1": (2, "Bottom performer")}, results)
        check("KPI12 KYC transition detected",
              [tuple(r) for r in one("SELECT from_status, to_status FROM kpi12_kyc_transitions WHERE customer_id = 'CKA'")],
              [("Pending", "Verified")], results)
        check("KPI8 uses KYC at transaction time (CKA was Pending)",
              one("SELECT DISTINCT kyc_status FROM v_txn WHERE customer_id = 'CKA'")[0][0], "Pending", results)
        tx.rollback()

    with engine.connect() as conn:
        # Independent cross-check of the window function with a self-join on the REAL data.
        window_count = conn.execute(text(
            "SELECT COUNT(*) FROM kpi05_suspicious_transactions WHERE txns_in_10_min >= 3")).scalar()
        join_count = conn.execute(text(
            "SELECT COUNT(*) FROM (SELECT a.transaction_id FROM fact_transaction a JOIN fact_transaction b "
            "ON b.account_id = a.account_id AND b.txn_ts BETWEEN a.txn_ts - INTERVAL 10 MINUTE AND a.txn_ts "
            "GROUP BY a.transaction_id HAVING COUNT(*) >= 3) x")).scalar()
        check("KPI5 window count = self-join count (real data)", window_count, join_count, results)
        left = conn.execute(text("SELECT COUNT(*) FROM fact_transaction WHERE source_batch_id = 'test'")).scalar()
        check("fixture rolled back", left, 0, results)

        print("\nReal-data row counts per KPI view:")
        for view in VIEWS:
            print(f"  {view:32} {conn.execute(text(f'SELECT COUNT(*) FROM {view}')).scalar():5}")
        print("\nKPI 1 top 5:")
        for r in conn.execute(text("SELECT * FROM kpi01_top_customers")):
            print("  ", tuple(r))
    print(f"\n{sum(results)}/{len(results)} checks passed")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
