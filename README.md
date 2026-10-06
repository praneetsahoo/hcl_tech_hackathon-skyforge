# RetailBank Customer Transaction Analytics — Team SkyForge

HCLTech AI-Cloud Data Engineering Hackathon. An AWS pipeline that turns RetailBank's messy raw files into a trusted star schema and 13 KPIs, and applies a Day 2 incremental drop (new records, changed customers/products/branches, late corrections) without reprocessing history or double-counting.

**Live dashboard:** http://3.107.194.103 (login password in SSM `/retailbank/dashboard/password`)

| Document | What it covers |
| --- | --- |
| [docs/DESIGN.md](docs/DESIGN.md) | Architecture, flowchart, AWS choices, decisions and assumptions |
| [docs/DATA_PROFILE.md](docs/DATA_PROFILE.md) | Profile of the raw files before cleaning |
| [docs/DQ_REPORT.md](docs/DQ_REPORT.md) | Data-quality / rejects report: what was found and how it was handled |
| [SECURITY.md](SECURITY.md) | Security controls (verified) and known MVP limitations |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | Demo flow, who says what, likely SME questions |
| [infra/README.md](infra/README.md) | AWS resources and deployment |

## Architecture

```
Raw files ──► S3 bronze ──► Python/pandas on EC2 (silver) ──► RDS MySQL (gold) ──► KPI SQL views ──► Streamlit
(Day 1/2)    as received    clean + validate + dedupe          star schema, SCD2     13 KPIs            dashboard :80
                            bad rows ──► quarantine (table + S3) with reason codes
Across every layer: private VPC · IAM role (no keys) · passwords in SSM · CloudWatch logs + ERROR alarm
```

| Layer | Where | What happens |
| --- | --- | --- |
| Bronze | S3 `bronze/batch=<id>/` | Files saved byte-for-byte, with name, row count, SHA-256 and batch in `ingestion_log` |
| Silver | `pipeline/cleanse.py` on EC2 | One pure function per entity; fixes formats; quarantines what can't be fixed |
| Gold | RDS MySQL `retailbank` | `dim_customer`, `dim_product`, `dim_branch` (SCD Type 2) + `fact_transaction` (one row per transaction_id) |
| KPI | `sql/kpi_views.sql` | One view per KPI; the dashboard reads only these |

## Run it

```bash
# on EC2 (deployed by infra/deploy.sh)
retailbank-run day1                 # Day 1 baseline load
retailbank-run day2                 # Day 2 incremental drop

# locally
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q       # 87 tests, no AWS needed
```

Each run: S3 bronze → profile/parse (broken JSON salvaged) → clean + validate → quarantine → **one database transaction** (SCD2 merge, fact upsert, DQ scorecard, KPI snapshot) → audit row in `pipeline_runs`. Any error rolls the whole batch back, marks the run FAILED, and logs ERROR to CloudWatch (alarm `retailbank-pipeline-errors`).

## Incremental design (Day 2)

- **Dimensions, SCD Type 2.** A business-meaningful change (KYC status, address, email/phone, product price/status, branch manager) closes the current row (`valid_to`, `is_current = 0`) and inserts a new version. Minor fixes (e.g. a name typo) update in place. Unchanged rows are skipped.
- **Late corrections.** A Day 2 row re-using a transaction_id **updates the existing fact row**. Old and new values go to `fact_correction_log`. The primary key makes a second fact row impossible.
- **Point-in-time.** Each fact links to the dimension version valid on the transaction date, so a September transaction stays linked to the customer's September KYC status.
- **Idempotent.** A SHA-256 "fingerprint" of each row decides insert / update / version / skip, so re-running a batch changes nothing.
- **Order-safe.** Re-running an *older* batch never overwrites rows a *newer* batch already changed.

Verified on AWS: Day 1 → Day 2 → Day 2 again → Day 1 again gives 880 facts / 880 unique IDs, 11 corrections, 0 changes on the re-runs.

## Key decisions

| Decision | Why |
| --- | --- |
| Python/pandas on EC2, not Glue/Spark | Data is MB-sized; Spark adds ~1 min start-up per run and a Glue connection to private RDS for no benefit. Same stages would move to Glue at GB–TB scale |
| RDS MySQL, not DynamoDB/Redshift | KPIs need JOINs, window functions and RANK; MySQL answers them in under a second at this size |
| S3 for raw files | Keeps files exactly as received (even broken JSON) for audit and replay |
| One Python orchestrator, not Step Functions | One pipeline on one server; ordered steps with stop-on-failure and one transaction are enough |
| Quarantine conflicting transactions, keep the best copy of duplicate master data | Never guess money; master-data re-sends are safe to collapse |
| Timestamp is the source of truth for transaction time | The `date` column disagrees in 296 rows; rejecting them would lose ~25% of data, so they are flagged |
| Money KPIs = successful INR transactions, refunds subtracted | Failed/pending transactions moved no money; non-INR rows have no rate, so they are flagged for review |

## Project layout

```
pipeline/   config, readers, rules, cleanse (silver), load (gold), ingest (bronze), run_pipeline, db
sql/        schema.sql (tables) · kpi_views.sql (13 KPIs)
dashboard/  app.py (Streamlit)
infra/      deploy.sh, systemd unit, CloudWatch agent config, run wrapper
scripts/    check_schema, check_kpis, check_dashboard, failure_drills, dq_report, reset_data
tests/      87 pytest tests
data/raw/   source files exactly as received (day1, day2)
```

## Testing

- **87 pytest tests:** cleaning rules, dedupe, quarantine routing, count reconciliation, SCD2, corrections, out-of-order safety, dashboard login and failure display.
- **Live checks on AWS:** schema constraints (10/10), KPI known answers (10/10), dashboard render (all 8 tabs, 0 errors), failure drills (8/8, including mid-load crash rollback and the alarm firing).

## AI use

We used Claude as an AI engineering assistant to speed up design and implementation. The team reviewed the architecture, ran every step on AWS, and checked results against the data. The tests caught real bugs in generated code, for example a phone validation error and an out-of-order re-run issue, which were fixed and locked in with tests.
