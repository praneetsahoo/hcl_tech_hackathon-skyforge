"""Run the whole pipeline for ONE batch, in order, stopping on failure.

    python -m pipeline.run_pipeline --batch-id day1

Steps: bronze (S3) -> silver (clean + validate + quarantine) ->
gold (SCD2 dimensions + fact upsert) -> data-quality outputs.
The database work is ONE transaction: on any error everything is rolled
back, the run is marked FAILED, and an ERROR line goes to CloudWatch.
Re-running the same batch is safe (idempotent).
"""
import argparse
import logging
import sys
import uuid
from datetime import date, datetime

from sqlalchemy import text

from pipeline.cleanse import apply_referential_checks, process_entity
from pipeline.config import BATCH_DATES, BATCH_FILES, get_settings, raw_path
from pipeline.db import get_engine
from pipeline.ingest import file_metadata, upload_bronze, upload_quarantine
from pipeline.load import dimension_keys, load_facts, merge_dimension, write_dq, write_kpi_snapshot

log = logging.getLogger("pipeline")


def setup_logging(log_file: str = ""):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


def run(batch_id: str, settings=None, engine=None) -> dict:
    """Run one batch. Returns a summary dict; raises on failure."""
    settings = settings or get_settings()
    if batch_id not in BATCH_FILES:
        raise ValueError(f"unknown batch_id {batch_id!r}")
    batch_date = date.fromisoformat(BATCH_DATES[batch_id])
    run_id = str(uuid.uuid4())
    log.info("run %s started for batch %s", run_id, batch_id)

    try:
        engine = engine or get_engine(settings)
        with engine.begin() as conn:        # audit row survives even if the load fails
            conn.execute(text("INSERT INTO pipeline_runs (run_id, batch_id, started_at, status) "
                              "VALUES (:r, :b, :t, 'RUNNING')"), {"r": run_id, "b": batch_id, "t": datetime.now()})
    except Exception as exc:                # e.g. database unreachable: nothing was changed
        log.error("ERROR run %s for batch %s could not start (database unreachable?): %s", run_id, batch_id, exc)
        raise
    try:
        from pipeline.readers import read_source
        # ---- bronze + silver ----
        results, ingestion = {}, []
        for entity, file_name in BATCH_FILES[batch_id].items():
            path = raw_path(settings, batch_id, entity)
            if not path.exists() or path.stat().st_size == 0:
                raise FileNotFoundError(f"source file missing or empty: {path}")
            rows, structural_error, lost = read_source(path)
            if structural_error:
                log.warning("%s: structural defect (%s); %d records salvaged, %d fragments lost",
                            file_name, structural_error, len(rows), len(lost))
            meta = file_metadata(path, row_count=len(rows))
            meta.update(batch_id=batch_id, entity=entity,
                        s3_key=upload_bronze(settings, batch_id, path, meta["sha256"]))
            ingestion.append(meta)
            results[entity] = process_entity(entity, file_name, rows, lost, batch_date)
            if structural_error:
                results[entity].flag(["MALFORMED_JSON_SALVAGED"])

        # ---- gold: one transaction ----
        with engine.begin() as conn:
            dim_counts = {e: merge_dimension(conn, e, results[e].clean, batch_id, batch_date)
                          for e in ("customers", "products", "branches")}
            apply_referential_checks(results["transactions"], *dimension_keys(conn))
            fact_counts = load_facts(conn, results["transactions"].clean, batch_id, run_id)
            write_dq(conn, batch_id, run_id, list(results.values()), ingestion)
            write_kpi_snapshot(conn, batch_id)

            totals = {
                "rows_read": sum(r.received for r in results.values()),
                "rows_loaded": sum(r.passed for r in results.values()),
                "rows_rejected": sum(len(r.quarantine) for r in results.values()),
                "rows_duplicate": sum(r.duplicates for r in results.values()),
                "rows_corrected": fact_counts["corrected"],
            }
            for r in results.values():                       # check, not assert
                if r.received != r.passed + len(r.quarantine) + r.duplicates:
                    raise RuntimeError(f"{r.source_file}: counts do not reconcile")
            conn.execute(text(
                "UPDATE pipeline_runs SET status = 'SUCCESS', finished_at = :t, rows_read = :rows_read, "
                "rows_loaded = :rows_loaded, rows_rejected = :rows_rejected, rows_duplicate = :rows_duplicate, "
                "rows_corrected = :rows_corrected WHERE run_id = :r"),
                {**totals, "t": datetime.now(), "r": run_id})

        for entity, r in results.items():
            upload_quarantine(settings, batch_id, entity, r.quarantine)
        summary = {"run_id": run_id, "batch_id": batch_id, **totals,
                   "dimensions": dim_counts, "facts": fact_counts}
        log.info("run %s SUCCESS: %s", run_id, summary)
        return summary

    except Exception as exc:
        log.error("ERROR run %s for batch %s failed: %s", run_id, batch_id, exc, exc_info=True)
        with engine.begin() as conn:
            conn.execute(text("UPDATE pipeline_runs SET status = 'FAILED', finished_at = :t, "
                              "error_message = :e WHERE run_id = :r"),
                         {"t": datetime.now(), "e": str(exc)[:2000], "r": run_id})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the RetailBank pipeline for one batch")
    parser.add_argument("--batch-id", required=True, choices=sorted(BATCH_FILES))
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_file)
    try:
        summary = run(args.batch_id, settings)
    except Exception as exc:
        logging.getLogger("pipeline").error("ERROR pipeline stopped: %s", exc)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
