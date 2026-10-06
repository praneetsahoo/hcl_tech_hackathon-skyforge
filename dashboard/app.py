"""RetailBank KPI dashboard (Streamlit).

Every number on this page is read from the KPI SQL views in MySQL; nothing
is hard-coded. The login password is read from SSM Parameter Store.
Run:  streamlit run dashboard/app.py
"""
import hmac
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # import the pipeline package
from pipeline.config import BATCH_FILES, get_settings              # noqa: E402
from pipeline.db import get_engine                                 # noqa: E402

log = logging.getLogger("dashboard")
st.set_page_config(page_title="RetailBank Analytics", layout="wide")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dashboard_password() -> str:
    local = os.environ.get("RB_DASHBOARD_PASSWORD")
    if local:                                   # local development only
        return local
    import boto3
    settings = get_settings()
    ssm = boto3.client("ssm", region_name=settings.aws_region)
    return ssm.get_parameter(Name=settings.dashboard_password_param, WithDecryption=True)["Parameter"]["Value"]


@st.cache_resource
def engine():
    return get_engine()


@st.cache_data(ttl=60, show_spinner=False)
def query(sql: str) -> pd.DataFrame:
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn)


def inr(value) -> str:
    return "–" if value is None or pd.isna(value) else f"₹{float(value):,.0f}"


def spreadsheet_safe(df: pd.DataFrame) -> bytes:
    """CSV download that cannot run formulas when opened in Excel."""
    safe = df.astype(str).apply(lambda col: col.map(
        lambda v: "'" + v if v[:1] in ("=", "+", "-", "@") else v))
    return safe.to_csv(index=False).encode()


def how(text_: str):
    st.caption(f"How this works: {text_}")


# ---------------------------------------------------------------------------
# Login gate
# ---------------------------------------------------------------------------

def login() -> bool:
    if st.session_state.get("authenticated"):
        return True
    st.title("RetailBank Analytics")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        try:
            expected = dashboard_password()
        except Exception:
            log.exception("could not read dashboard password")
            st.error("Login is unavailable right now (could not read the password store).")
            return False
        if hmac.compare_digest(entered.encode(), expected.encode()):
            st.session_state["authenticated"] = True
            st.rerun()
        time.sleep(1)                           # slow down password guessing
        st.error("Incorrect password.")
    return False


# ---------------------------------------------------------------------------
# Sidebar: run a batch through the same pipeline code
# ---------------------------------------------------------------------------

def sidebar():
    st.sidebar.header("Pipeline")
    batch_id = st.sidebar.selectbox("Batch", sorted(BATCH_FILES))
    if st.sidebar.button("Run batch", type="primary"):
        from pipeline.run_pipeline import run
        with st.spinner(f"Running {batch_id}: S3 → clean → quarantine → MySQL…"):
            try:
                summary = run(batch_id, engine=engine())
                st.sidebar.success(
                    f"{batch_id} done: {summary['facts']['inserted']} new, "
                    f"{summary['facts']['corrected']} corrected, {summary['rows_rejected']} quarantined.")
            except Exception as exc:
                log.exception("batch run failed")
                st.sidebar.error(f"Run failed and was rolled back: {str(exc)[:200]}")
        query.clear()
    st.sidebar.caption("Re-running a batch is safe: unchanged rows are skipped.")
    if st.sidebar.button("Log out"):
        st.session_state.clear()
        st.rerun()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

def tab_overview():
    totals = query("SELECT COUNT(*) AS txns, SUM(net_inr) AS net_value FROM v_txn_value").iloc[0]
    all_txns = query("SELECT COUNT(*) AS n FROM fact_transaction").iloc[0]["n"]
    dq = query("SELECT SUM(records_received) AS recv, SUM(records_passed) AS passed, "
               "SUM(records_rejected) AS rejected FROM dq_scorecard").iloc[0]
    last = query("SELECT batch_id, status, finished_at FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Transactions loaded", f"{all_txns:,}")
    c2.metric("Net value (successful, INR)", inr(totals["net_value"]))
    c3.metric("Records quarantined", f"{int(dq['rejected'] or 0):,}")
    c4.metric("Pass rate", f"{100 * (dq['passed'] or 0) / max(dq['recv'] or 1, 1):.1f}%")
    c5.metric("Last run", f"{last.iloc[0]['batch_id']} · {last.iloc[0]['status']}" if len(last) else "none")

    st.subheader("Monthly net value by region")
    monthly = query("SELECT region, month_start, net_value_inr FROM kpi02_region_monthly")
    if len(monthly):
        st.line_chart(monthly.pivot(index="month_start", columns="region", values="net_value_inr"))
    how("raw files land in S3, Python on EC2 cleans them, MySQL stores a star schema, and each chart "
        "is a SQL view. Money figures count successful INR transactions with refunds subtracted.")


def tab_customers():
    st.subheader("KPI 1 · Top 5 customers by net transaction value")
    st.dataframe(query("SELECT * FROM kpi01_top_customers"), hide_index=True, use_container_width=True)
    how("SUM of net value per customer (refunds subtracted), ranked with RANK(); ties go to more transactions.")

    st.subheader("KPI 6 · Customer segments (RFM-lite)")
    seg = query("SELECT segment, COUNT(*) AS customers FROM kpi06_customer_segments GROUP BY segment")
    left, right = st.columns([1, 2])
    left.bar_chart(seg.set_index("segment"))
    right.dataframe(query("SELECT * FROM kpi06_customer_segments ORDER BY rfm_total DESC"),
                    hide_index=True, use_container_width=True, height=300)
    how("NTILE(3) scores Recency, Frequency and Monetary 1-3; the total (3-9) sets "
        "Platinum (8-9), Gold (6-7), Silver (4-5) or Bronze (3).")

    st.subheader("KPI 4 · Dormant accounts (no transaction in 90 days)")
    dormant = query("SELECT * FROM kpi04_dormant_accounts ORDER BY dormancy_class DESC, days_inactive DESC")
    st.metric("High-risk dormant (Credit Card / Loan)", int((dormant["dormancy_class"] == "High-risk dormant").sum()))
    st.dataframe(dormant, hide_index=True, use_container_width=True, height=300)


def tab_branches():
    st.subheader("KPI 7 · Branch ranking within region")
    ranking = query("SELECT * FROM kpi07_branch_ranking ORDER BY region, rank_in_region")
    st.dataframe(ranking[ranking["performer"].notna()], hide_index=True, use_container_width=True)
    with st.expander("All branches"):
        st.dataframe(ranking, hide_index=True, use_container_width=True)
    how("RANK() OVER (PARTITION BY region ORDER BY revenue DESC): rank 1 is the top performer, "
        "the lowest rank is the bottom performer.")

    st.subheader("KPI 2 · Monthly trend per branch with month-over-month growth")
    branches = query("SELECT DISTINCT branch_id FROM kpi02_branch_monthly ORDER BY branch_id")["branch_id"]
    choice = st.selectbox("Branch", branches)
    trend = query("SELECT * FROM kpi02_branch_monthly ORDER BY branch_id, month_start")
    trend = trend[trend["branch_id"] == choice]
    st.bar_chart(trend.set_index("month_start")["net_value_inr"])
    st.dataframe(trend, hide_index=True, use_container_width=True)
    how("LAG() gets the previous month's value for the same branch; growth = (this − previous) / previous.")


def tab_products():
    st.subheader("KPI 3 · Revenue share by product type, quarter over quarter")
    rev = query("SELECT * FROM kpi03_product_revenue ORDER BY quarter, product_type")
    st.bar_chart(rev.pivot(index="quarter", columns="product_type", values="revenue_inr"))
    st.dataframe(rev, hide_index=True, use_container_width=True)
    how("share = revenue / SUM(revenue) OVER (PARTITION BY quarter); QoQ growth uses LAG() per product type.")

    st.subheader("KPI 9 · Refund rate")
    left, right = st.columns(2)
    left.dataframe(query("SELECT * FROM kpi09_refund_by_product"), hide_index=True, use_container_width=True)
    right.dataframe(query("SELECT * FROM kpi09_refund_by_branch ORDER BY refund_rate_pct DESC"),
                    hide_index=True, use_container_width=True, height=300)


def tab_risk():
    st.subheader("KPI 5 · Suspicious transactions")
    sus = query("SELECT * FROM kpi05_suspicious_transactions ORDER BY txn_ts DESC")
    reasons = ["HIGH_VALUE", "VELOCITY_3_IN_10_MIN", "ODD_HOURS"]
    cols = st.columns(3)
    for col, reason in zip(cols, reasons):
        col.metric(reason, int(sus["risk_reason"].str.contains(reason).sum()))
    picked = st.multiselect("Filter by reason", reasons, default=reasons)
    st.dataframe(sus[sus["risk_reason"].apply(lambda r: any(p in r for p in picked))],
                 hide_index=True, use_container_width=True, height=350)
    how("velocity uses a sliding window: COUNT(*) OVER (PARTITION BY account ORDER BY time "
        "RANGE INTERVAL 10 MINUTE PRECEDING). High value is over ₹1,00,000; odd hours is 00:00–04:59.")

    st.subheader("KPI 8 · KYC risk exposure")
    kyc = query("SELECT * FROM kpi08_kyc_exposure")
    at_risk = kyc[kyc["at_risk"] == 1]
    c1, c2 = st.columns(2)
    c1.metric("Transactions by non-verified customers", f"{at_risk['share_of_count_pct'].sum():.1f}%")
    c2.metric("Value by non-verified customers", f"{at_risk['share_of_value_pct'].sum():.1f}%  ({inr(at_risk['value_inr'].sum())})")
    st.dataframe(kyc, hide_index=True, use_container_width=True)
    how("uses the KYC status the customer had at the time of each transaction (SCD2 point-in-time).")


def tab_data_quality():
    st.subheader("KPI 10 · Data-quality scorecard")
    st.dataframe(query("SELECT * FROM kpi10_dq_scorecard ORDER BY batch_id, source_file"),
                 hide_index=True, use_container_width=True)
    how("every file balances: received = passed + rejected + duplicates removed. Nothing is silently dropped.")

    st.subheader("Issues found (rejected and fixed/flagged)")
    st.dataframe(query("SELECT * FROM dq_issue_counts ORDER BY batch_id, source_file, severity, record_count DESC"),
                 hide_index=True, use_container_width=True, height=250)

    st.subheader("Quarantine (rejected records with reasons)")
    q = query("SELECT batch_id, entity, source_file, record_key, reason_codes, raw_record, quarantined_at "
              "FROM dq_quarantine ORDER BY batch_id, entity, record_key")
    c1, c2 = st.columns(2)
    entity = c1.selectbox("Entity", ["all"] + sorted(q["entity"].unique().tolist()))
    search = c2.text_input("Search (ID or reason)")
    if entity != "all":
        q = q[q["entity"] == entity]
    if search:
        q = q[q["record_key"].fillna("").str.contains(search, case=False, regex=False)
              | q["reason_codes"].str.contains(search, case=False, regex=False)]
    st.dataframe(q, hide_index=True, use_container_width=True, height=350)
    st.download_button("Download quarantine (CSV)", spreadsheet_safe(q), "quarantine.csv", "text/csv")


def tab_day2():
    st.subheader("KPI 11 · Day-over-day incremental reconciliation")
    left, right = st.columns([1, 2])
    left.dataframe(query("SELECT * FROM kpi11_incremental_counts"), hide_index=True, use_container_width=True)
    deltas = query("SELECT * FROM kpi11_kpi_deltas ORDER BY kpi_name, day2_rank")
    if len(deltas):
        right.dataframe(deltas, hide_index=True, use_container_width=True, height=300)
    else:
        right.info("Day 1 vs Day 2 KPI comparison appears after both batches have run with snapshots.")
    how("a corrected transaction UPDATEs its existing row (old values go to the correction log), "
        "so KPI totals never double-count.")

    st.subheader("Corrected transactions (late-arriving corrections)")
    st.dataframe(query("SELECT transaction_id, batch_id, old_values, new_values, corrected_at "
                       "FROM fact_correction_log ORDER BY corrected_at"), hide_index=True, use_container_width=True)

    st.subheader("KPI 12 · KYC status transitions (SCD Type 2)")
    st.dataframe(query("SELECT * FROM kpi12_kyc_transitions ORDER BY customer_id"), hide_index=True,
                 use_container_width=True)
    how("each change closes the old version (valid_to) and opens a new one (valid_from); "
        "a self-join on old.valid_to = new.valid_from finds the transitions.")

    st.subheader("KPI 13 · New account activations in Day 2")
    st.dataframe(query("SELECT * FROM kpi13_new_activations ORDER BY first_txn_ts"), hide_index=True,
                 use_container_width=True)


def tab_runs():
    st.subheader("Pipeline runs")
    st.dataframe(query("SELECT batch_id, status, started_at, finished_at, rows_read, rows_loaded, rows_rejected, "
                       "rows_duplicate, rows_corrected, error_message FROM pipeline_runs ORDER BY started_at DESC"),
                 hide_index=True, use_container_width=True)
    st.subheader("Files landed in S3 (bronze)")
    st.dataframe(query("SELECT * FROM ingestion_log ORDER BY batch_id, file_name"), hide_index=True,
                 use_container_width=True)
    how("each run writes an audit row; a failed run is marked FAILED with the error, and its database "
        "changes are rolled back. Errors also go to CloudWatch with an alarm.")


TABS = {"Overview": tab_overview, "Customers": tab_customers, "Branches": tab_branches,
        "Products": tab_products, "Risk & KYC": tab_risk, "Data quality": tab_data_quality,
        "Day 2 changes": tab_day2, "Pipeline runs": tab_runs}


def main():
    if not login():
        return
    st.title("RetailBank Customer Transaction Analytics")
    sidebar()
    for tab, render in zip(st.tabs(list(TABS)), TABS.values()):
        with tab:
            try:
                render()
            except Exception:
                log.exception("tab failed")
                st.error("This section could not load: the database may be unreachable. "
                         "Please try again in a minute.")


main()
