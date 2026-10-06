"""Phase 11: deliberately break the pipeline and prove it fails SAFELY
(gold tables unchanged) and VISIBLY (FAILED run row + ERROR in the log
that CloudWatch reads). Drills use a temporary copy of the input files and
S3 upload switched off, so real data and bronze files are never touched.
Usage (on EC2, as the service user):  python -m scripts.failure_drills
"""
import dataclasses
import shutil
import tempfile
from datetime import date
from pathlib import Path

from sqlalchemy import text

import pipeline.run_pipeline as rp
from pipeline.cleanse import process_entity
from pipeline.config import get_settings
from pipeline.db import get_engine
from pipeline.readers import read_json_salvaging

RESULTS = []


def report(name, ok, detail):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def snapshot(engine):
    """Counts that must NOT change when a run fails."""
    with engine.connect() as c:
        return {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                for t in ("fact_transaction", "dim_customer", "dim_product", "dim_branch",
                          "fact_correction_log", "dq_quarantine", "dq_scorecard")}


def last_run(engine):
    with engine.connect() as c:
        return tuple(c.execute(text("SELECT status, LEFT(error_message, 90) FROM pipeline_runs "
                                    "ORDER BY started_at DESC LIMIT 1")).one())


def temp_copy(settings, batch_id):
    root = Path(tempfile.mkdtemp(prefix="rb_drill_"))
    shutil.copytree(settings.data_dir / "raw" / batch_id, root / "raw" / batch_id)
    return root


def expect_failure(name, engine, settings, batch_id, check_error):
    before = snapshot(engine)
    try:
        rp.run(batch_id, settings, engine)
        report(name, False, "run did NOT fail")
        return
    except Exception as exc:
        error = str(exc)
    status, message = last_run(engine)
    unchanged = snapshot(engine) == before
    report(name, status == "FAILED" and unchanged and check_error(error),
           f"run={status}, gold unchanged={unchanged}, error='{error[:80]}'")


def main():
    base = get_settings()
    rp.setup_logging(base.log_file)          # ERROR lines go to the CloudWatch log file
    engine = get_engine(base)
    no_s3 = dataclasses.replace(base, s3_bucket="")
    real_before = snapshot(engine)

    # 1. a source file is missing
    root = temp_copy(base, "day2")
    (root / "raw/day2/customer_updates_day2.csv").unlink()
    expect_failure("1 missing source file", engine, dataclasses.replace(no_s3, data_dir=root), "day2",
                   lambda e: "missing or empty" in e)

    # 2. a source file is empty (0 bytes)
    root = temp_copy(base, "day2")
    (root / "raw/day2/transactions_day2.csv").write_text("")
    expect_failure("2 empty source file", engine, dataclasses.replace(no_s3, data_dir=root), "day2",
                   lambda e: "missing or empty" in e)

    # 3. crash in the MIDDLE of the gold load -> the whole batch must roll back
    root = temp_copy(base, "day2")
    with open(root / "raw/day2/transactions_day2.csv", "a") as f:     # one extra, valid transaction
        f.write("T9999,A0069,P014,B041,2026-10-01,2026-10-01 12:00:00,1500,UPI,Success,INR,Drill,False\n")
    original = rp.write_kpi_snapshot

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after facts were written")
    rp.write_kpi_snapshot = crash
    try:
        expect_failure("3 crash mid-load rolls back", engine, dataclasses.replace(no_s3, data_dir=root), "day2",
                       lambda e: "simulated crash" in e)
    finally:
        rp.write_kpi_snapshot = original
    with engine.connect() as c:
        leaked = c.execute(text("SELECT COUNT(*) FROM fact_transaction WHERE transaction_id = 'T9999'")).scalar()
    report("3b new row from failed run not kept", leaked == 0, f"T9999 rows in fact = {leaked}")

    # 4. database unreachable
    bad_db = dataclasses.replace(no_s3, db_host="127.0.0.1", db_port=1)
    code = None
    try:
        rp.run("day2", bad_db)  # noqa
    except Exception as exc:
        code = type(exc).__name__
    report("4 database unreachable", code is not None and snapshot(engine) == real_before,
           f"failed fast with {code}; real data unchanged")

    # 5a. a batch with no files in the landing zone
    code = rp.main(["--batch-id", "day9", "--batch-date", "2026-12-01"])
    report("5a batch with no files fails cleanly", code == 1 and last_run(engine)[0] == "FAILED",
           f"exit code {code}, run={last_run(engine)[0]}")
    # 5b. an unsafe batch id is refused before anything runs
    try:
        rp.main(["--batch-id", "../etc"])
        report("5b unsafe batch id rejected", False, "accepted")
    except SystemExit as exc:
        report("5b unsafe batch id rejected", exc.code == 2, f"exit code {exc.code} (refused by argparse)")

    # 6. truncated JSON (an object cut off mid-way) -> quarantined, not a crash
    text_ = (base.data_dir / "raw/day2/products_day2.json").read_text()
    cut = text_[: text_.index('"P099"') + 20]            # chop inside the P099 object
    records, error, lost = read_json_salvaging(cut)
    result = process_entity("products", "products_day2.json", records, lost, date(2026, 10, 1))
    reasons = {q["reason_codes"] for q in result.quarantine}
    report("6 truncated JSON salvaged + fragment quarantined", bool(error) and "MALFORMED_JSON" in reasons,
           f"{len(records)} objects recovered, {len(lost)} fragment(s) quarantined")

    report("ALL real data untouched", snapshot(engine) == real_before, str(snapshot(engine)))
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} drills passed")
    raise SystemExit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
