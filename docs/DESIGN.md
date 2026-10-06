# RetailBank Transaction Analytics — Design Document

Team SkyForge · HCLTech AI-Cloud Data Engineering Hackathon · 6 Oct 2026

## 1. Problem and objective

We build an automated AWS pipeline that turns RetailBank's messy daily source files into a trusted star schema and 13 KPIs, and absorbs a Day 2 incremental drop without reprocessing history or double-counting.

**Problem.** Core banking, card and UPI/Net Banking systems drop CSV and JSON files into a landing zone. They are inconsistent, incomplete and not analytics-ready, so the bank cannot trust numbers for marketing, fraud monitoring, branch performance or KYC risk.

**Objective.**

1. Land every file in a bronze zone exactly as received, with ingestion metadata.
2. Profile, cleanse, standardise and validate; quarantine what cannot be fixed, with a reason code.
3. Model a curated layer: Customer, Product and Branch dimensions plus a transaction-grain fact.
4. Compute KPIs 1–10 on Day 1 and KPIs 11–13 after Day 2, repeatably.
5. Apply Day 2 as upserts with SCD Type 2 history and in-place fact corrections.

**Users.**

| User | What they need |
| --- | --- |
| Bank analytics team | KPIs, segments, branch rankings |
| Risk / compliance | Suspicious transactions, KYC exposure |
| Data engineering / ops | Batch runs, DQ scorecard, rejected records to investigate |

**MVP outcome.** One command (or one dashboard button) runs a batch end to end: S3 bronze, cleansing and quarantine, MySQL star schema, KPI views, and a Streamlit dashboard showing the results and the data-quality scorecard.

## 2. Requirements

Every requirement below comes from the problem statement; anything we had to interpret is listed under Assumptions (section 13).

**Functional.**

| # | Requirement | Source |
| --- | --- | --- |
| F1 | Land each file in bronze exactly as received; record file name, row count, ingestion timestamp, batch/day ID | 5.1 |
| F2 | Profile each entity, then cleanse, standardise and validate per the 5.2 rules | 5.2 |
| F3 | Route failed records to quarantine with a reason code, never silently drop | 5.2 |
| F4 | Curated star schema: dim_customer, dim_product, dim_branch + fact_transaction at transaction grain, keys and grain documented | 5.3 |
| F5 | Day 2: dimension upserts with SCD Type 2 for business-meaningful changes | 5.4 |
| F6 | Day 2: late transaction corrections update the existing fact row, no extra facts | 5.4 |
| F7 | KPIs 1–10 (Day 1) and 11–13 (Day 2) | 6 |
| F8 | DQ scorecard per file per run: received, passed, rejected, top 3 reasons (KPI 10) | 5.6 |

**Non-functional.**

- **Idempotent and parameterised:** every run takes a `batch_id`; re-running a batch creates no duplicates.
- **Automated DQ checks in the pipeline:** row counts, null checks, referential checks.
- **Version control:** Git with a README covering setup, design decisions and the incremental approach.
- **Justified cloud choice:** AWS, every service explained.
- **Secure, observable, low cost:** private database, no secrets in code, logs and alarms, free-tier sized resources.

## 3. Source data and expected issues

Eight files arrive in two drops; the issues below are the ones the problem statement warns about, and will be confirmed by profiling the real files before the cleansing rules are frozen.

| File | Day | Entity | Format | Expected issues |
| --- | --- | --- | --- | --- |
| branches.csv | 1 | Branch dimension | CSV | Re-sent duplicates, missing manager names, inconsistent region/branch_type, bad contact numbers |
| customers.csv | 1 | Customer dimension | CSV | Exact + near duplicates (whitespace, casing), bad emails/phones, mixed gender and KYC values, future/implausible DOB, several registration_date formats, two customers sharing one account_id |
| products.json | 1 | Product dimension | JSON | At least one structural defect (truncated/concatenated feed), duplicate product_id, negative prices, category casing, is_active as bool/string/missing/null |
| transactions.csv | 1 | Transaction fact (~3 months) | CSV | Exact and conflicting duplicate IDs; amounts blank, negative, zero, comma-formatted or with currency symbol; non-INR rows; date vs timestamp disagree; several date formats; orphan account/branch/product IDs; mixed payment_method, status, is_refund; future-dated rows |
| branches_day2.csv | 2 | Branch updates | CSV | One new branch, one manager change |
| customer_updates_day2.csv | 2 | Customer updates + new | CSV | KYC progression, address changes, newly onboarded customers |
| products_day2.json | 2 | Product updates + new | JSON | Price/status changes, new products |
| transactions_day2.csv | 2 | New + corrected facts | CSV | New transactions plus re-used transaction_ids with corrected status/amount |

**Open item:** step 1 of the build is a profiling script that prints, per file, row counts, null counts, distinct values of every categorical column and sample bad rows; the cleansing rules in section 8 are adjusted to what it finds.

## 4. Architecture

Files land unchanged in S3, a Python pipeline on EC2 cleanses and models them into RDS MySQL inside a private subnet, and a Streamlit app on the same EC2 serves the KPIs.

```
                                   ┌──────────────── VPC ─────────────────────────────────┐
┌──────────────┐  upload  ┌───────────────┐    │ ┌─────────────────────┐    ┌───────────────────────┐ │
│ Source files │ ───────▶ │ S3 bronze zone│ ───┼▶│ Python pipeline     │ ─▶ │ RDS MySQL (private)   │ │
│ Day 1 + Day 2│          │ raw, unchanged│    │ │ on EC2, per batch_id│    │ SCD2 star schema      │ │
│ 6 CSV, 2 JSON│          │ + quarantine/ │    │ │ profile, cleanse,   │    │ quarantine, scorecard │ │
└──────────────┘          └───────────────┘    │ │ dedupe, quarantine  │    │ 13 KPI SQL views      │ │
                                               │ └─────────────────────┘    └──────────┬────────────┘ │
                          ┌───────────────┐    │ ┌─────────────────────┐   SQL views    │              │
                          │ Bank analysts │ ◀──┼─│ Streamlit app       │ ◀──────────────┘              │
                          │ browser+login │    │ │ same EC2, port 80   │                               │
                          └───────────────┘    │ └─────────────────────┘                               │
                                               └──────────────────────────────────────────────────────┘
   Across the stack: IAM role on EC2 (no keys) · SSM Parameter Store for passwords · CloudWatch logs + ERROR alarm
```

Read left to right: the source drop is uploaded to the S3 bronze zone; the pipeline reads that batch, quarantines what it cannot fix and loads the star schema into MySQL in one transaction; the KPI views feed the Streamlit app, which analysts open in a browser. IAM, SSM and CloudWatch apply to every step.

## 5. End-to-end data pipeline

One Python orchestrator, `run_pipeline.py --batch-id day1`, runs these stages in order and stops at the first failure; every stage is safe to re-run for the same batch.

1. **Ingest (bronze).** Copy each source file unchanged to `s3://retailbank-data-<account>/bronze/batch=<batch_id>/<file>`. Write one `ingestion_log` row per file: file name, row count, size, checksum, ingested_at, batch_id. Same checksum + batch already logged → skip (idempotent).
2. **Profile.** For each file, compute row count, nulls per column, distinct values of categorical columns, format patterns of dates and amounts. Saved as a JSON profile in `s3://…/profiles/batch=<id>/` and shown on the dashboard.
3. **Parse.** Read CSV as text (no type guessing). Parse JSON defensively: a structurally broken document is split into the objects that do parse; the unparseable fragment goes to quarantine as `MALFORMED_JSON`.
4. **Cleanse and validate (silver).** One pure Python function per rule per entity: trim, standardise casing and codes, parse amounts and dates, validate formats. A record either comes out clean or with a list of reason codes.
5. **Deduplicate.** Exact duplicates kept once (counted). Near duplicates resolved after normalisation. Conflicting duplicate transaction IDs within one batch quarantined.
6. **Referential checks.** Transactions whose account, branch or product does not exist in the dimensions → quarantine `ORPHAN_<KEY>`.
7. **Quarantine.** Every rejected record lands in `dq_quarantine` (MySQL) and `s3://…/quarantine/batch=<id>/`, with the original raw record and every reason code.
8. **Load (gold).** In ONE database transaction: SCD2 merge into the three dimensions, upsert into `fact_transaction` keyed on transaction_id, write corrections to `fact_correction_log`, write the DQ scorecard and the `pipeline_runs` audit row. Any error → rollback, nothing half-loaded.
9. **Serve (KPIs).** 13 KPI SQL views over the star schema; the Streamlit dashboard reads only these views.

## 6. AWS service decisions

We use seven AWS services, each tied to one job; Glue, Redshift, Lambda and Step Functions are deliberately left out because at megabyte scale they add start-up time, cost and moving parts without changing the result.

| Component | Used? | Responsibility | Why chosen | Alternative considered | When the alternative wins |
| --- | --- | --- | --- | --- | --- |
| Amazon S3 | Yes | Bronze zone (raw files as received), profiles, quarantine copies | Cheap, durable, versioned; raw files kept for replay and audit | Store raw rows only in RDS | Never for raw files: RDS cannot hold a file "exactly as received" or a broken JSON |
| EC2 (t3.micro, reused) | Yes | Runs the Python pipeline and the always-on Streamlit dashboard | A dashboard needs a running server; one box keeps deploy simple | Lambda for the pipeline | Event-driven, short jobs; would add VPC endpoints and a second deploy path for no gain here |
| RDS MySQL 8 (reused instance, new `retailbank` database) | Yes | Silver/gold tables, quarantine, scorecard, KPI views | Star schema needs JOINs, window functions, RANK, transactions for safe upserts | DynamoDB | Key-value access at huge scale; it has no JOINs or window functions |
| VPC, subnets, security groups (reused) | Yes | RDS in private subnets; only the app SG reaches port 3306 | Database never exposed to the internet | Public RDS | Never for production data |
| IAM role on EC2 | Yes | Least-privilege access to the new bucket, its SSM secrets and log group | No access keys on the server or in Git | IAM user keys in a .env file | Never |
| CloudWatch | Yes | Pipeline and dashboard logs; ERROR metric filter + alarm | We see every failed run without logging into the box | Log files only on EC2 | Never once more than one person operates it |
| SSM Parameter Store | Yes | Database passwords as SecureStrings | Free, IAM-controlled, no secrets in code | Secrets Manager | When automatic password rotation is required |
| AWS Glue / EMR | No | — | Data is MB-sized; Spark adds ~1 min start per run and needs a Glue connection to reach private RDS | Glue job per stage | At GB–TB scale or many parallel feeds |
| Amazon Redshift | No | — | MySQL answers all 13 KPIs in well under a second at this size | Redshift as gold warehouse | TB-scale analytics, many concurrent analysts |
| AWS Lambda | No (optional extra) | Could auto-start a run when a file lands in S3 | Runs are started deliberately with a batch_id | S3 → Lambda trigger | When feeds arrive unattended on a schedule |
| Step Functions / MWAA | No | — | One Python orchestrator with ordered steps, retry and stop-on-failure is enough | Step Functions state machine | Many services, branching and retries across teams |

## 7. Data model

The gold layer is a star schema: three SCD Type 2 dimensions with surrogate keys and one fact table whose grain is one row per transaction_id.

| Table | Grain | Primary key | Business key | Key columns |
| --- | --- | --- | --- | --- |
| dim_customer | One row per customer version | customer_sk (surrogate, auto-increment) | customer_id | name, email, phone, gender, dob, kyc_status, address, account_id, registration_date, valid_from, valid_to, is_current, row_hash |
| dim_product | One row per product version | product_sk | product_id | product_name, category, price, is_active, valid_from, valid_to, is_current, row_hash |
| dim_branch | One row per branch version | branch_sk | branch_id | branch_name, region, branch_type, manager_name, contact_number, valid_from, valid_to, is_current, row_hash |
| fact_transaction | **One row per transaction_id** | transaction_id | transaction_id | customer_sk, product_sk, branch_sk, account_id, txn_ts, txn_date, amount_inr, currency, payment_method, status, is_refund, needs_review, source_batch_id, last_updated_batch_id |

**Supporting tables.**

| Table | Purpose |
| --- | --- |
| ingestion_log | One row per file per batch: name, row count, checksum, ingested_at (F1) |
| dq_quarantine | Every rejected record: entity, batch, raw record, reason codes |
| dq_scorecard | Per file per run: received, passed, rejected, duplicates, top reasons (KPI 10) |
| fact_correction_log | Old vs new values of every corrected transaction (KPI 11, audit) |
| pipeline_runs | Audit row per run: batch, start/end, status, counts, error |

**Design choices.**

- Surrogate keys let one customer have several versions; the fact points at the version current at transaction time (point-in-time join on valid_from/valid_to).
- `row_hash` (hash of the tracked attributes) makes change detection one comparison instead of column-by-column.
- Money is `DECIMAL(15,2)`, never float.
- Indexes on fact (customer_sk, txn_ts), (branch_sk, txn_date), (product_sk) and dimension business keys + is_current, matching the KPI queries.

## 8. Data quality and quarantine

Each rule is either a fix (standardise and keep) or a reject (quarantine with a reason code); nothing is silently dropped, and every file reconciles as received = passed + rejected + duplicates removed.

| Entity | Fix (keep the record) | Reject (reason code) |
| --- | --- | --- |
| Customers | Trim and case-normalise names/emails; gender and KYC to one code set (e.g. `verified`, `VERIFIED`, `V` → Verified); registration_date to ISO from all formats; collapse exact and near duplicates | `INVALID_EMAIL`, `INVALID_PHONE` (flag, keep if other fields OK), `INVALID_DOB` (future or age > 120), `MISSING_CUSTOMER_ID`, `SHARED_ACCOUNT_ID` (second claimant) |
| Products | Category casing; is_active from true/false/"yes"/"1"/null → one boolean (null → false, flagged) | `MALFORMED_JSON`, `DUPLICATE_PRODUCT_ID` (conflicting), `NEGATIVE_PRICE` |
| Branches | Region and branch_type to one code set; missing manager → `UNKNOWN`, flagged | `INVALID_CONTACT` (flag), `DUPLICATE_BRANCH` (conflicting re-send) |
| Transactions | Amount parsing (strip ₹, Rs, commas); dates from all formats; payment_method, status, is_refund to one code set; non-INR kept with `needs_review` | `MISSING_AMOUNT`, `INVALID_AMOUNT` (zero/negative non-refund), `CONFLICTING_DUPLICATE`, `DATE_MISMATCH` (date vs timestamp), `FUTURE_DATED`, `ORPHAN_ACCOUNT`, `ORPHAN_BRANCH`, `ORPHAN_PRODUCT` |

**Automated checks every run.** Row-count reconciliation per file, not-null on all keys, referential integrity fact → dimensions, no duplicate transaction_id in the fact, and a `pipeline_runs` status of `FAILED` if any check breaks.

**Investigating and reprocessing.** `dq_quarantine` keeps the original raw record and all reasons, filterable on the dashboard. A fixed record can be re-submitted in a later batch and flows through the same rules.

## 9. Day 2 incremental design

Day 2 runs the same pipeline with `--batch-id day2`; only the new files are read, dimensions are merged as SCD Type 2, and corrections overwrite the existing fact row, so history is never reprocessed and nothing is double-counted.

**Dimension merge (SCD Type 2), per incoming record:**

1. Business key not in the dimension → insert as a new current row (`valid_from` = batch date, `valid_to` = 9999-12-31, `is_current` = 1).
2. Key exists and `row_hash` unchanged → do nothing (this is what makes re-runs safe).
3. Key exists and a tracked attribute changed (KYC status, address, product price/status, branch manager) → close the current row (`valid_to` = batch date, `is_current` = 0) and insert a new current row.
4. Only cosmetic differences that cleansing already removes (case, spaces) never create a version.

**Fact corrections (late-arriving):**

1. transaction_id not in the fact → insert (new transaction).
2. transaction_id exists with a different payload → write old and new values to `fact_correction_log`, then `UPDATE` the fact row and set `last_updated_batch_id`. One row per transaction_id is enforced by the primary key, so KPIs can never double-count.
3. transaction_id exists with an identical payload → skip.

**Idempotency.** Re-running `day2` finds identical hashes and identical fact payloads, so it inserts 0 rows and logs the run as a no-op. `ingestion_log` also records file checksums per batch.

**Incremental KPIs.** KPI 11 counts new, updated and corrected records from the Day 2 run and compares KPI 1 and KPI 7 before vs after (snapshotted per batch in `kpi_snapshot`). KPI 12 reads customers with more than one dim_customer version where kyc_status changed. KPI 13 finds accounts whose first transaction is in batch day2, and Day 2 customers with no earlier transactions.

## 10. KPI design

Each KPI is one SQL view over the star schema, so the dashboard, tests and panel all see the same numbers; value KPIs use INR rows that passed validation, with refunds netted out.

| # | KPI | Core SQL technique | Priority |
| --- | --- | --- | --- |
| 1 | Top 5 customers by net transaction volume | SUM(CASE WHEN is_refund THEN -amount ELSE amount END), ORDER BY net DESC, txn_count DESC, LIMIT 5 | Must |
| 2 | Monthly trend per branch and region, MoM growth % | GROUP BY month, branch, region + LAG() OVER (PARTITION BY branch ORDER BY month) | Must |
| 3 | Product revenue share, period-over-period | SUM() / SUM() OVER () for share; LAG over period (month, if the data spans less than two quarters) | Should |
| 4 | Dormant accounts (90 days), high-risk if Credit Card or Loan | MAX(txn_date) per account vs as-of date; LEFT JOIN products; CASE for high-risk | Should |
| 5 | High-value / suspicious transactions with reason | amount > 100000 OR COUNT(*) OVER (PARTITION BY account ORDER BY txn_ts RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW) >= 3 OR HOUR(txn_ts) BETWEEN 0 AND 4; CONCAT_WS of reasons | Must |
| 6 | RFM-lite segmentation | NTILE(3) for recency, frequency, monetary; score sum 8–9 Platinum, 6–7 Gold, 4–5 Silver, 3 Bronze | Should |
| 7 | Branch ranking within region, top and bottom | RANK() / DENSE_RANK() OVER (PARTITION BY region ORDER BY revenue DESC) | Must |
| 8 | KYC risk exposure | % and value of transactions WHERE kyc_status <> 'Verified' (after standardising) | Must |
| 9 | Refund rate by product and branch | Refund count and value / total, GROUP BY product, branch | Should |
| 10 | Data-quality scorecard | dq_scorecard + top 3 reasons via ROW_NUMBER() OVER (PARTITION BY file, batch ORDER BY count DESC) | Must |
| 11 | Day-over-day reconciliation | New / updated / corrected counts from the Day 2 run; KPI 1 and 7 before vs after from kpi_snapshot | Must (Day 2) |
| 12 | KYC status transition report | Self-join of dim_customer versions where kyc_status changed | Must (Day 2) |
| 13 | New account activation | First transaction per account in batch day2; Day 2 customers with no prior transactions | Must (Day 2) |

**Validation.** Each KPI query gets one hand-checked test case on a tiny fixture (e.g. 3 transactions within 10 minutes flags, 2 do not), and KPI 5's window result is cross-checked with a self-join.

## 11. Security

The database is never reachable from the internet, no password or key exists in code or Git, and each identity gets only the access its job needs.

| Who | Can access | What | Mechanism | Permission |
| --- | --- | --- | --- | --- |
| Analyst (browser) | Streamlit dashboard | KPI views, scorecard, quarantine | HTTP port 80 on EC2 + dashboard login (password in SSM) | Read and upload only |
| EC2 pipeline + dashboard | S3 bucket | bronze/, profiles/, quarantine/ prefixes | IAM role on the instance (no keys) | List + Get/Put on those prefixes only |
| EC2 pipeline + dashboard | SSM parameters | /retailbank/* | Same IAM role | GetParameter on that path only; explicit deny elsewhere |
| EC2 | RDS MySQL | `retailbank` database | Security group: 3306 only from the app SG; TLS required | App user with rights on retailbank.* only |
| EC2 | CloudWatch | /retailbank/* log groups | Same IAM role | Create stream + put events |
| Admin (team) | EC2 shell | Deploy and run commands | SSM Run Command (no SSH, no key pair) | Through the AWS console account |

**Data protection.** S3: public access blocked, SSE encryption, TLS-only bucket policy, versioning. RDS: encrypted at rest, private subnets, TLS in transit. The data is synthetic, but emails and phone numbers are still treated as PII: the dashboard shows them masked (e.g. `ra***@mail.com`).

**MVP limits we will state honestly.** The dashboard is HTTP, not HTTPS (production: ALB + ACM certificate); one shared dashboard login, not per-user accounts.

## 12. Failure handling and observability

A failure at any stage leaves the curated layer exactly as it was before the run, is logged to CloudWatch, and is visible in `pipeline_runs`; recovery is a re-run of the same batch.

| Stage | Failure | MVP response | Recovery |
| --- | --- | --- | --- |
| Source | File missing or empty | Run stops before loading; ERROR logged; run marked FAILED | Manual: supply file, re-run batch |
| Source | Malformed JSON / broken CSV line | Salvage parseable records; fragment quarantined `MALFORMED_JSON` / `MALFORMED_ROW` | Automatic for good records; bad fragment reviewed in quarantine |
| Ingestion | S3 upload fails | Retry 3 times with backoff, then FAILED | Re-run batch (checksum makes it idempotent) |
| Validation | Bad records | Quarantined with reason codes; run continues | Fix at source, re-submit in a later batch |
| Load | Database error mid-load | Whole batch is one transaction → rollback, nothing half-loaded | Re-run batch once the DB is back |
| Database | RDS unreachable before start | Connection check fails fast, ERROR logged, alarm | Re-run after recovery |
| Dashboard | App crashes | systemd restarts it automatically | Automatic |
| Dashboard | Database down | Friendly error message instead of a stack trace | Automatic when DB returns |

**What we watch.**

- CloudWatch log groups `/retailbank/pipeline` and `/retailbank/dashboard` (shipped by the CloudWatch agent).
- Metric filter on `ERROR` → alarm `retailbank-pipeline-errors`.
- `pipeline_runs`: per run start/end, duration, status, rows read / loaded / rejected / corrected.
- `dq_scorecard`: rejection rates per file; a spike is the first sign of an upstream problem.

**First thing to check if a run fails:** the dashboard's Pipeline runs tab (status and error text), then the CloudWatch log for that run.

## 13. Assumptions

The problem statement leaves these points open; we picked the simplest defensible reading of each and will confirm them with the panelist.

| # | Ambiguity | Our assumption |
| --- | --- | --- |
| A1 | Conflicting duplicate transaction_id inside one batch | Quarantine every version as `CONFLICTING_DUPLICATE`; we cannot know which is true |
| A2 | Day 2 row re-using a Day 1 transaction_id | It is a correction; the later batch wins and the old values go to `fact_correction_log` |
| A3 | Non-INR transactions | Loaded with `needs_review = 1`, excluded from rupee-value KPIs, listed for business review |
| A4 | "Current processing date" | The batch run date passed as a parameter; transactions after it are `FUTURE_DATED` |
| A5 | "Last 90 days" for dormant accounts | Counted back from the latest transaction date in the data, so results are repeatable |
| A6 | Two customers sharing one account_id | The earlier-registered customer owns it; the other is quarantined `SHARED_ACCOUNT_ID` |
| A7 | KPI 3 "current vs prior quarter" with ~3 months of data | Use the latest month vs the prior month if a full prior quarter is not present |
| A8 | RFM tier boundaries | NTILE(3) per dimension, scores summed: 8–9 Platinum, 6–7 Gold, 4–5 Silver, 3 Bronze |
| A9 | Which changes are SCD Type 2 | Customer KYC status, address, email/phone; product price and status; branch manager. Cleansing-level differences never create versions |
| A10 | Refunds in revenue KPIs | Refunds subtract from value; refund rows count in transaction counts |
| A11 | Date vs timestamp disagree | Timestamp is the source of truth if it parses; otherwise `DATE_MISMATCH` |
| A12 | Missing manager name | Filled as `UNKNOWN` and flagged, not rejected |

## 14. MVP scope, team split and plan

We build the smallest end-to-end slice first (Day 1 → bronze → cleanse → star schema → KPIs → dashboard), then add Day 2 incremental logic, then the remaining KPIs.

**Scope.**

| Must have | Should have | Later / not in MVP |
| --- | --- | --- |
| Bronze landing + ingestion_log; Day 1 cleansing + quarantine; star schema; KPIs 1, 2, 5, 7, 8, 10; Day 2 SCD2 + corrections; KPIs 11–13; Streamlit dashboard; README | KPIs 3, 4, 6, 9; PII masking on dashboard; profile view | S3-triggered Lambda; HTTPS; per-user login; Glue/Redshift migration |

**Team split (everyone must be able to explain every part).**

| Member | Owns | Also reviews |
| --- | --- | --- |
| A | Ingestion, profiling, DQ rules and quarantine | Data model |
| B | Data model, SCD2 merge, fact corrections | DQ rules |
| C | KPI SQL views and their tests | Dashboard |
| D | AWS setup, deployment, Streamlit dashboard | KPI SQL |

**Implementation steps.**

1. Foundation: repo structure, config, requirements, profiling script on the real files.
2. AWS: new S3 bucket, `retailbank` database + app user, IAM paths, SSM secrets, log groups; move the reference dashboard to port 8080.
3. Data layer: schema DDL for dimensions, fact and DQ tables.
4. Day 1 ETL: ingest, parse, cleanse, dedupe, referential checks, quarantine, load.
5. KPI views 1–10 with known-answer tests.
6. Tests: pytest on cleansing rules, SCD2 merge, correction upsert, KPI 5 window.
7. Dashboard: upload/run batch, KPIs, scorecard, quarantine, pipeline runs.
8. Deploy to EC2 and run Day 1 end to end.
9. Day 2: run the incremental batch, KPIs 11–13, re-run to prove idempotency.
10. Failure drills, security review, README, demo rehearsal.

**Deployment.** Code lives in `praneetsahoo/hcl_tech_hackathon-skyforge`. An idempotent deploy script on the existing EC2 (via SSM Run Command) pulls the repo, installs requirements in a venv, writes the env file (no secrets, only SSM paths), applies the schema, and starts the Streamlit service on port 80. Source files are uploaded through the dashboard or `run_pipeline.py`, which lands them in S3 first.

## 15. Sprint 0 pitch (about 4 minutes)

A script any team member can deliver; split it into four parts so each person presents one and can answer on all.

**Part 1 — Problem and goal (Member A).** "RetailBank receives daily files from core banking, cards and UPI that are inconsistent and incomplete, so the analytics team can't trust the numbers. Our goal is an automated pipeline that lands the raw files, cleans and validates them, models a star schema, and computes 13 KPIs. On Day 2 it has to absorb new data, changed customers and corrected transactions without reprocessing history or double-counting."

**Part 2 — Pipeline and data quality (Member B).** "Every file first lands unchanged in S3, our bronze zone, with a log of file name, row count and batch ID. A Python pipeline on EC2 profiles each file, then applies one rule per issue: standardising KYC codes, parsing rupee amounts with commas or symbols, unifying date formats. Anything we can't fix goes to a quarantine table with a reason code, like ORPHAN_ACCOUNT or CONFLICTING_DUPLICATE. Nothing is silently dropped: received always equals passed plus rejected plus duplicates, and that's our data-quality scorecard, KPI 10."

**Part 3 — Model, incremental and AWS choices (Member C).** "Clean data goes into RDS MySQL as three dimensions and one fact table at transaction grain. Dimensions are SCD Type 2: when a customer's KYC status changes on Day 2, we close the old row and add a new one, so history is kept. A corrected transaction updates the existing fact row, and the old values go to a correction log, so KPIs never double-count. Each batch loads in one database transaction and is safe to re-run. We chose MySQL over DynamoDB because the KPIs need joins, window functions and ranking. We didn't use Glue or Redshift because our data is megabytes; Spark would add start-up time and cost with no benefit. We'd switch to them at gigabyte-to-terabyte scale."

**Part 4 — Security, monitoring and demo (Member D).** "RDS sits in a private subnet and only our app's security group can reach it, over TLS. The EC2 server uses an IAM role, so there are no access keys anywhere, and passwords live in SSM Parameter Store. Logs go to CloudWatch with an alarm on errors, and every run writes an audit row. For the demo, we'll upload Day 1, show the KPIs and the quarantine, then run Day 2 and show the KYC transitions, the corrected transactions and that a re-run changes nothing. We used Claude to speed up the build; we review, test and can explain everything it generated."

**Questions to confirm with the panelist:** assumptions A1 (conflicting duplicates quarantined), A3 (non-INR flagged, excluded from value KPIs) and A7 (KPI 3 falls back to month-over-month).
