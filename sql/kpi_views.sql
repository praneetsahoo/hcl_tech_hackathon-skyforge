-- RetailBank KPI layer: one SQL view per KPI. Safe to re-run (CREATE OR REPLACE).
-- Money KPIs use SUCCESSFUL INR transactions only; refunds are subtracted.
-- (No percent signs in this file: they clash with the Python DB driver.)

-- ---------------------------------------------------------------------
-- Base view: every fact joined to the dimension version that was valid
-- at transaction time (point-in-time via the *_sk keys), plus net value.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_txn AS
SELECT f.transaction_id, f.account_id, f.customer_id, f.product_id, f.branch_id,
       f.txn_ts, f.txn_date,
       CAST(DATE_SUB(f.txn_date, INTERVAL DAY(f.txn_date) - 1 DAY) AS DATE) AS month_start,
       CONCAT(YEAR(f.txn_date), '-Q', QUARTER(f.txn_date))                 AS quarter,
       f.amount, f.currency, f.amount_inr, f.status, f.is_refund, f.payment_method,
       f.needs_review, f.source_batch_id,
       CASE WHEN f.is_refund = 1 THEN -f.amount_inr ELSE f.amount_inr END     AS net_inr,
       c.customer_name, c.kyc_status,
       p.product_name, p.product_type, p.category,
       b.branch_name, b.region
FROM fact_transaction f
JOIN dim_customer c ON c.customer_sk = f.customer_sk
JOIN dim_product  p ON p.product_sk  = f.product_sk
JOIN dim_branch   b ON b.branch_sk   = f.branch_sk;

-- Successful INR transactions: the population for every money KPI.
CREATE OR REPLACE VIEW v_txn_value AS
SELECT * FROM v_txn WHERE status = 'Success' AND amount_inr IS NOT NULL;

-- ---------------------------------------------------------------------
-- KPI 1: Top 5 customers by net transaction value (ties: more transactions win)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi01_top_customers AS
WITH totals AS (
    SELECT customer_id, SUM(net_inr) AS net_value_inr, COUNT(*) AS txn_count
    FROM v_txn_value
    GROUP BY customer_id
)
SELECT RANK() OVER (ORDER BY t.net_value_inr DESC, t.txn_count DESC) AS rank_no,
       t.customer_id, c.customer_name, c.account_id, t.net_value_inr, t.txn_count
FROM totals t
JOIN dim_customer c ON c.customer_id = t.customer_id AND c.is_current = 1
ORDER BY rank_no
LIMIT 5;

-- ---------------------------------------------------------------------
-- KPI 2: Monthly volume and value per branch, with month-over-month growth
-- (LAG compares each month with the branch's previous month of activity)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi02_branch_monthly AS
WITH monthly AS (
    SELECT region, branch_id, branch_name, month_start,
           COUNT(*) AS txn_count, SUM(net_inr) AS net_value_inr
    FROM v_txn_value
    GROUP BY region, branch_id, branch_name, month_start
)
SELECT region, branch_id, branch_name, month_start, txn_count, net_value_inr,
       LAG(net_value_inr) OVER w AS prev_month_value_inr,
       ROUND(100 * (net_value_inr - LAG(net_value_inr) OVER w)
             / NULLIF(LAG(net_value_inr) OVER w, 0), 1) AS mom_growth_pct
FROM monthly
WINDOW w AS (PARTITION BY branch_id ORDER BY month_start);

-- KPI 2 rolled up to region level.
CREATE OR REPLACE VIEW kpi02_region_monthly AS
WITH monthly AS (
    SELECT region, month_start, COUNT(*) AS txn_count, SUM(net_inr) AS net_value_inr
    FROM v_txn_value
    GROUP BY region, month_start
)
SELECT region, month_start, txn_count, net_value_inr,
       ROUND(100 * (net_value_inr - LAG(net_value_inr) OVER w)
             / NULLIF(LAG(net_value_inr) OVER w, 0), 1) AS mom_growth_pct
FROM monthly
WINDOW w AS (PARTITION BY region ORDER BY month_start);

-- ---------------------------------------------------------------------
-- KPI 3: Revenue share by product type, quarter over quarter
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi03_product_revenue AS
WITH q AS (
    SELECT quarter, product_type, SUM(net_inr) AS revenue_inr, COUNT(*) AS txn_count
    FROM v_txn_value
    GROUP BY quarter, product_type
)
SELECT quarter, product_type, revenue_inr, txn_count,
       ROUND(100 * revenue_inr / SUM(revenue_inr) OVER (PARTITION BY quarter), 1) AS share_of_quarter_pct,
       LAG(revenue_inr) OVER (PARTITION BY product_type ORDER BY quarter)         AS prev_quarter_revenue_inr,
       ROUND(100 * (revenue_inr - LAG(revenue_inr) OVER (PARTITION BY product_type ORDER BY quarter))
             / NULLIF(LAG(revenue_inr) OVER (PARTITION BY product_type ORDER BY quarter), 0), 1) AS qoq_growth_pct
FROM q;

-- KPI 3 detail: revenue share per individual product (all time).
CREATE OR REPLACE VIEW kpi03_product_share AS
SELECT product_id, product_name, product_type, category,
       SUM(net_inr) AS revenue_inr,
       ROUND(100 * SUM(net_inr) / SUM(SUM(net_inr)) OVER (), 2) AS share_pct
FROM v_txn_value
GROUP BY product_id, product_name, product_type, category;

-- ---------------------------------------------------------------------
-- KPI 4: Dormant accounts (no transaction in the 90 days before the latest
-- date in the data); high-risk when the account's product is a Credit Card or Loan.
-- LEFT JOIN keeps accounts that never transacted at all.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi04_dormant_accounts AS
WITH as_of AS (SELECT MAX(txn_date) AS as_of_date FROM fact_transaction),
last_txn AS (
    SELECT account_id, txn_date, product_id,
           ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY txn_ts DESC) AS rn
    FROM fact_transaction
)
SELECT c.account_id, c.customer_id, c.customer_name,
       l.txn_date AS last_txn_date,
       DATEDIFF(a.as_of_date, l.txn_date) AS days_inactive,
       p.product_type,
       CASE WHEN p.product_type IN ('Credit Card', 'Loan') THEN 'High-risk dormant'
            ELSE 'Dormant' END AS dormancy_class
FROM dim_customer c
CROSS JOIN as_of a
LEFT JOIN last_txn l    ON l.account_id = c.account_id AND l.rn = 1
LEFT JOIN dim_product p ON p.product_id = l.product_id AND p.is_current = 1
WHERE c.is_current = 1
  AND (l.txn_date IS NULL OR l.txn_date < a.as_of_date - INTERVAL 90 DAY);

-- ---------------------------------------------------------------------
-- KPI 5: Suspicious transactions. A row is flagged if ANY rule fires:
--   amount over 1,00,000 INR | 3+ transactions from the same account in a
--   10-minute sliding window | time between 00:00 and 04:59.
-- The window is a true RANGE window over the timestamp, per account.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi05_suspicious_transactions AS
WITH scored AS (
    SELECT t.*,
           COUNT(*) OVER (PARTITION BY account_id ORDER BY txn_ts
                          RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW) AS txns_in_10_min
    FROM v_txn t
)
SELECT transaction_id, account_id, customer_id, customer_name, txn_ts, amount, currency, status,
       txns_in_10_min,
       CONCAT_WS(' | ',
           CASE WHEN amount_inr > 100000 THEN 'HIGH_VALUE' END,
           CASE WHEN txns_in_10_min >= 3 THEN 'VELOCITY_3_IN_10_MIN' END,
           CASE WHEN HOUR(txn_ts) < 5 THEN 'ODD_HOURS' END) AS risk_reason
FROM scored
WHERE amount_inr > 100000 OR txns_in_10_min >= 3 OR HOUR(txn_ts) < 5;

-- ---------------------------------------------------------------------
-- KPI 6: RFM-lite segmentation. NTILE(3) gives each customer a score 1-3 for
-- Recency (more recent = 3), Frequency and Monetary; the sum (3-9) sets the tier.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi06_customer_segments AS
WITH rfm AS (
    SELECT customer_id,
           DATEDIFF((SELECT MAX(txn_date) FROM fact_transaction), MAX(txn_date)) AS recency_days,
           COUNT(*)     AS frequency,
           SUM(net_inr) AS monetary_inr
    FROM v_txn_value
    GROUP BY customer_id
),
scored AS (
    SELECT rfm.*,
           NTILE(3) OVER (ORDER BY recency_days DESC) AS r_score,
           NTILE(3) OVER (ORDER BY frequency ASC)     AS f_score,
           NTILE(3) OVER (ORDER BY monetary_inr ASC)  AS m_score
    FROM rfm
)
SELECT s.customer_id, c.customer_name, s.recency_days, s.frequency, s.monetary_inr,
       s.r_score, s.f_score, s.m_score, s.r_score + s.f_score + s.m_score AS rfm_total,
       CASE WHEN s.r_score + s.f_score + s.m_score >= 8 THEN 'Platinum'
            WHEN s.r_score + s.f_score + s.m_score >= 6 THEN 'Gold'
            WHEN s.r_score + s.f_score + s.m_score >= 4 THEN 'Silver'
            ELSE 'Bronze' END AS segment
FROM scored s
JOIN dim_customer c ON c.customer_id = s.customer_id AND c.is_current = 1;

-- ---------------------------------------------------------------------
-- KPI 7: Branch revenue ranking within each region (top and bottom performer)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi07_branch_ranking AS
WITH rev AS (
    SELECT region, branch_id, branch_name, SUM(net_inr) AS revenue_inr, COUNT(*) AS txn_count
    FROM v_txn_value
    GROUP BY region, branch_id, branch_name
),
ranked AS (
    SELECT rev.*,
           RANK()       OVER (PARTITION BY region ORDER BY revenue_inr DESC) AS rank_in_region,
           DENSE_RANK() OVER (PARTITION BY region ORDER BY revenue_inr DESC) AS dense_rank_in_region,
           RANK()       OVER (PARTITION BY region ORDER BY revenue_inr ASC)  AS rank_from_bottom
    FROM rev
)
SELECT region, branch_id, branch_name, revenue_inr, txn_count, rank_in_region, dense_rank_in_region,
       CASE WHEN rank_in_region = 1   THEN 'Top performer'
            WHEN rank_from_bottom = 1 THEN 'Bottom performer' END AS performer
FROM ranked;

-- ---------------------------------------------------------------------
-- KPI 8: KYC risk exposure: share of transactions (count and value) done by
-- customers whose KYC was NOT Verified at the time of the transaction.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi08_kyc_exposure AS
SELECT kyc_status,
       COUNT(*)     AS txn_count,
       SUM(net_inr) AS value_inr,
       ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)         AS share_of_count_pct,
       ROUND(100 * SUM(net_inr) / SUM(SUM(net_inr)) OVER (), 1) AS share_of_value_pct,
       kyc_status <> 'Verified'                                  AS at_risk
FROM v_txn_value
GROUP BY kyc_status;

-- ---------------------------------------------------------------------
-- KPI 9: Refund rate by product type and by branch
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi09_refund_by_product AS
SELECT product_type,
       COUNT(*) AS txn_count, SUM(is_refund) AS refund_count,
       ROUND(100 * SUM(is_refund) / COUNT(*), 1) AS refund_rate_pct,
       SUM(CASE WHEN is_refund = 1 THEN amount_inr ELSE 0 END) AS refund_value_inr,
       ROUND(100 * SUM(CASE WHEN is_refund = 1 THEN amount_inr ELSE 0 END) / SUM(amount_inr), 1) AS refund_value_pct
FROM v_txn_value
GROUP BY product_type;

CREATE OR REPLACE VIEW kpi09_refund_by_branch AS
SELECT region, branch_id, branch_name,
       COUNT(*) AS txn_count, SUM(is_refund) AS refund_count,
       ROUND(100 * SUM(is_refund) / COUNT(*), 1) AS refund_rate_pct,
       SUM(CASE WHEN is_refund = 1 THEN amount_inr ELSE 0 END) AS refund_value_inr
FROM v_txn_value
GROUP BY region, branch_id, branch_name;

-- ---------------------------------------------------------------------
-- KPI 10: Data-quality scorecard per file and batch, with the top 3 reject reasons
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi10_dq_scorecard AS
WITH reasons AS (
    SELECT batch_id, source_file, reason_code, record_count,
           ROW_NUMBER() OVER (PARTITION BY batch_id, source_file
                              ORDER BY record_count DESC, reason_code) AS rn
    FROM dq_issue_counts
    WHERE severity = 'REJECT'
)
SELECT s.batch_id, s.source_file, s.records_received, s.records_passed, s.records_rejected,
       s.duplicates_removed,
       ROUND(100 * s.records_passed / s.records_received, 1) AS pass_rate_pct,
       GROUP_CONCAT(CONCAT(r.reason_code, ' (', r.record_count, ')') ORDER BY r.rn SEPARATOR ', ')
           AS top_3_reject_reasons
FROM dq_scorecard s
LEFT JOIN reasons r ON r.batch_id = s.batch_id AND r.source_file = s.source_file AND r.rn <= 3
GROUP BY s.batch_id, s.source_file, s.records_received, s.records_passed, s.records_rejected,
         s.duplicates_removed;

-- ---------------------------------------------------------------------
-- KPI 11: Day-over-day incremental reconciliation for the LATEST batch
-- (by business date), so it works for day2, day3, ... without changes.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_latest_batch AS
SELECT batch_id, batch_date
FROM batch_registry
ORDER BY batch_date DESC, batch_id DESC
LIMIT 1;

CREATE OR REPLACE VIEW kpi11_incremental_counts AS
SELECT l.batch_id, 'New transactions' AS metric,
       (SELECT COUNT(*) FROM fact_transaction f WHERE f.source_batch_id = l.batch_id) AS record_count
FROM v_latest_batch l
UNION ALL
SELECT l.batch_id, 'Corrected transactions',
       (SELECT COUNT(DISTINCT c.transaction_id) FROM fact_correction_log c WHERE c.batch_id = l.batch_id)
FROM v_latest_batch l
UNION ALL
SELECT l.batch_id, 'New customers',
       (SELECT COUNT(*) FROM dim_customer d WHERE d.batch_id = l.batch_id AND d.valid_from = '1900-01-01')
FROM v_latest_batch l
UNION ALL
SELECT l.batch_id, 'Customers with new version',
       (SELECT COUNT(*) FROM dim_customer d WHERE d.valid_from = l.batch_date)
FROM v_latest_batch l
UNION ALL
SELECT l.batch_id, 'New products',
       (SELECT COUNT(*) FROM dim_product d WHERE d.batch_id = l.batch_id AND d.valid_from = '1900-01-01')
FROM v_latest_batch l
UNION ALL
SELECT l.batch_id, 'Products with new version',
       (SELECT COUNT(*) FROM dim_product d WHERE d.valid_from = l.batch_date)
FROM v_latest_batch l
UNION ALL
SELECT l.batch_id, 'New branches',
       (SELECT COUNT(*) FROM dim_branch d WHERE d.batch_id = l.batch_id AND d.valid_from = '1900-01-01')
FROM v_latest_batch l
UNION ALL
SELECT l.batch_id, 'Branches with new version',
       (SELECT COUNT(*) FROM dim_branch d WHERE d.valid_from = l.batch_date)
FROM v_latest_batch l;

-- KPI 1 and KPI 7 after the latest batch vs after the previous batch (snapshots saved per batch).
CREATE OR REPLACE VIEW kpi11_kpi_deltas AS
WITH ordered AS (
    SELECT batch_id, ROW_NUMBER() OVER (ORDER BY batch_date DESC, batch_id DESC) AS rn
    FROM batch_registry
),
cur AS (SELECT batch_id FROM ordered WHERE rn = 1),
prev AS (SELECT batch_id FROM ordered WHERE rn = 2)
SELECT d2.kpi_name, d2.item_key, d2.item_label,
       (SELECT batch_id FROM prev) AS previous_batch, d2.batch_id AS latest_batch,
       d1.rank_no AS previous_rank, d2.rank_no AS latest_rank,
       d1.metric_value AS previous_value, d2.metric_value AS latest_value,
       d2.metric_value - COALESCE(d1.metric_value, 0) AS value_change
FROM kpi_snapshot d2
LEFT JOIN kpi_snapshot d1
       ON d1.kpi_name = d2.kpi_name AND d1.item_key = d2.item_key
      AND d1.batch_id = (SELECT batch_id FROM prev)
WHERE d2.batch_id = (SELECT batch_id FROM cur)
  AND (SELECT batch_id FROM prev) IS NOT NULL;

-- ---------------------------------------------------------------------
-- KPI 12: Customers whose KYC status changed (self-join of SCD2 versions:
-- the old version's valid_to equals the new version's valid_from)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi12_kyc_transitions AS
SELECT n.customer_id, n.customer_name, o.kyc_status AS from_status, n.kyc_status AS to_status,
       n.valid_from AS changed_on, n.batch_id
FROM dim_customer n
JOIN dim_customer o ON o.customer_id = n.customer_id AND o.valid_to = n.valid_from
WHERE o.kyc_status <> n.kyc_status;

-- ---------------------------------------------------------------------
-- KPI 13: Accounts whose FIRST ever transaction arrived in the LATEST batch,
-- and whether the customer was newly onboarded in that batch (no history before).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kpi13_new_activations AS
WITH firsts AS (
    SELECT account_id, transaction_id, txn_ts, source_batch_id,
           ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY txn_ts) AS rn
    FROM fact_transaction
),
first_version AS (
    SELECT customer_id, batch_id AS onboarded_batch
    FROM dim_customer
    WHERE valid_from = '1900-01-01'
)
SELECT f.source_batch_id AS batch_id, f.account_id, c.customer_id, c.customer_name,
       f.transaction_id AS first_transaction_id, f.txn_ts AS first_txn_ts,
       fv.onboarded_batch = f.source_batch_id AS newly_onboarded
FROM firsts f
JOIN v_latest_batch l ON l.batch_id = f.source_batch_id
JOIN dim_customer c ON c.account_id = f.account_id AND c.is_current = 1
JOIN first_version fv ON fv.customer_id = c.customer_id
WHERE f.rn = 1;
