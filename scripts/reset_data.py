"""Clear all RetailBank data so the batches can be replayed from scratch.

Only touches tables in the retailbank database; the raw files in S3 and
data/raw are untouched, so everything can be rebuilt by re-running batches.
Refuses to run without --yes.
Usage:  python -m scripts.reset_data --yes
"""
import sys

from sqlalchemy import text

from pipeline.db import get_engine

# children before parents (foreign keys)
TABLES = ["fact_correction_log", "fact_transaction", "dim_customer", "dim_product", "dim_branch",
          "dq_quarantine", "dq_scorecard", "dq_issue_counts", "ingestion_log", "kpi_snapshot", "pipeline_runs"]


def main():
    if "--yes" not in sys.argv:
        sys.exit("refusing to delete data without --yes")
    engine = get_engine()
    with engine.begin() as conn:
        if conn.execute(text("SELECT DATABASE()")).scalar() != "retailbank":
            sys.exit("not connected to the retailbank database; aborting")
        for table in TABLES:
            deleted = conn.execute(text(f"DELETE FROM {table}")).rowcount
            print(f"cleared {table}: {deleted} rows")
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"ALTER TABLE {table} AUTO_INCREMENT = 1"))


if __name__ == "__main__":
    main()
