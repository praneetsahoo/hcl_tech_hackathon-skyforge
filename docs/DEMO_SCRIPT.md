# Demo script (~15 minutes + questions)

**Open before you start:** the dashboard http://3.107.194.103 (logged in), the GitHub repo, and the flowchart in `docs/DESIGN.md`. Optional: AWS console tabs for S3 (`retailbank-data-748348797173`), CloudWatch alarm `retailbank-pipeline-errors`, Parameter Store.

Everything shown is live. No numbers are hard-coded.

---

## 1. Problem and architecture (Member A, ~2 min)

**Show:** the flowchart.

> "RetailBank gets daily files from core banking, cards and UPI that are messy: 9 spellings of 'status', amounts like 'Rs.81,250', four date formats, a broken JSON file. We built a pipeline that lands the raw files in S3, cleans them with Python on EC2, quarantines what can't be fixed with a reason, loads a star schema into MySQL on RDS, and computes 13 KPIs as SQL views shown in this Streamlit dashboard. Day 2 brings new data, changed customers and corrected transactions, and our pipeline absorbs it without reprocessing or double-counting."

Point at each box top to bottom: Source, S3 bronze, Python silver, Valid row?, quarantine, MySQL gold, KPI views, dashboard. Then the bottom security bar.

## 2. Raw data and data quality (Member A, ~3 min)

**Show:** dashboard → **Data quality** tab.

1. Scorecard: "Every file balances: received = passed + rejected + duplicates. Day 1 transactions: 940 received, 739 passed, 179 quarantined, 22 duplicates."
2. Issues table: "REJECT means quarantined, FLAG means fixed and kept. For example, 252 rows had a `date` that disagrees with the `timestamp`; we trust the timestamp and flag them rather than lose a quarter of the data."
3. Quarantine: type `CONFLICTING` in search. "Same transaction ID, different amounts. We don't guess money, so every version is quarantined with the original record."
4. Mention: `products.json` was missing a comma. "We salvaged all 114 records instead of crashing."

## 3. The star schema and KPIs (Member B and Member C, ~4 min)

**Member B, Customers tab:**
- KPI 1 top 5: "Net value: refunds subtracted, ties broken by transaction count."
- KPI 6 segments: "NTILE(3) on recency, frequency and monetary value; the sum gives Platinum to Bronze."
- KPI 4 dormant: "LEFT JOIN keeps accounts that never transacted; Credit Card and Loan accounts are high-risk."

**Member C:**
- **Branches tab:** KPI 7, "RANK() partitioned by region gives the top and bottom performer per region." KPI 2: pick a branch, "LAG() gives month-on-month growth."
- **Risk & KYC tab:** KPI 5, "the 10-minute rule is a real sliding window: COUNT over a RANGE of 10 minutes per account." KPI 8: "KYC status at the time of each transaction."
- **Products tab:** KPI 3, quarter-over-quarter revenue share.

## 4. Day 2 incremental (Member B, ~3 min) ← the most important part

**Show:** **Day 2 changes** tab.

1. KPI 11 counts: "141 new transactions, 11 corrections, 22 new customers, 16 customers with a new version."
2. Corrected transactions table: "T0577 changed from Failed to Success. We updated the same row and logged old and new values; there is still exactly one row for T0577."
3. KPI 12: "9 customers moved from Pending to Verified. SCD Type 2: we closed the old row and added a new one, so history is kept."
4. KPI 11 deltas: "C018 and C4718 entered the top 5 after Day 2."
5. **Live proof of idempotency:** sidebar, choose `day2`, click **Run batch**. Result: "0 new, 0 corrected." Then **Pipeline runs** tab: the new SUCCESS row.

> "We also tested re-running Day 1 *after* Day 2. Nothing was undone; 38 rows were protected because a newer batch had already changed them."

## 5. AWS, security and failure handling (Member D, ~3 min)

**Show:** Pipeline runs tab, then optionally the AWS console.

- "Files in S3 bronze are kept exactly as received, with a checksum, so we can always replay." (S3 console: `bronze/batch=day1/`)
- "RDS is in a private subnet; only our EC2's security group can reach it, over TLS."
- "No passwords in code: the server's IAM role reads them from Parameter Store at run time."
- "The whole gold load is one database transaction. In our failure drills we crashed it halfway: everything rolled back, the run was marked FAILED, and the CloudWatch alarm fired." (Pipeline runs tab shows the FAILED drill rows; the alarm history shows OK → ALARM.)
- "The dashboard runs as a systemd service; we killed it with kill -9 and it restarted itself."

## 6. Close (Member D, ~30 s)

> "To sum up: raw to trusted KPIs on AWS, with every bad record accounted for, Day 2 applied with history and corrections, safe re-runs, tested with 87 automated tests and live failure drills. We used Claude to speed up the build; we reviewed, tested and can explain everything, and our tests caught real bugs in the generated code."

---

## If something breaks during the demo

| Problem | What to do |
| --- | --- |
| Dashboard won't load | Say "it auto-restarts via systemd", wait 10 s, refresh. Fallback: show `docs/DQ_REPORT.md` and the KPI SQL on GitHub |
| Login fails | The password is in SSM `/retailbank/dashboard/password` (AWS console → Parameter Store → Show) |
| Run batch shows an error | It's rolled back by design; show the FAILED row in Pipeline runs, which demonstrates failure handling |

## Likely SME questions (short answers)

| Question | Answer |
| --- | --- |
| Why not Glue / Spark? | Data is megabytes; Spark adds ~1 min start-up and needs a Glue connection to private RDS. Same stages move to Glue at GB–TB scale |
| Why MySQL, not DynamoDB? | KPIs need JOINs, window functions and RANK; DynamoDB has none |
| Why both S3 and RDS? | S3 keeps the raw file exactly as received for audit and replay; RDS is for clean, queryable data |
| Why EC2, not Lambda? | The dashboard must run all the time; Lambda is for short event-driven jobs |
| How do you avoid double-counting corrections? | transaction_id is the primary key; a correction UPDATEs the row and logs old/new values |
| What is SCD Type 2? | Keep history by closing the old row (valid_to) and adding a new current row |
| What if a run fails halfway? | One transaction → full rollback, FAILED audit row, ERROR to CloudWatch, alarm |
| What happens at 10× / 100× data? | 10×: fine. 100×+: move cleaning to Glue/Spark with Parquet in S3, KPIs to Redshift or Athena, trigger with S3 events + Step Functions |
| Where is the AI component? | None in the product. Every requirement is deterministic data engineering; AI was used only to help build it |
| What did AI generate and how did you validate it? | Code and SQL. We validated with 87 tests, known-answer KPI checks, live AWS runs and failure drills; the tests caught a phone-validation bug and an out-of-order re-run bug |
| Security gaps? | HTTP not HTTPS, one shared login, shared EC2 role, no SNS on the alarm; all listed in SECURITY.md |
