"""Bronze layer: land each source file in S3 exactly as received.

The S3 key is fixed per batch and file, and the file's SHA-256 is stored as
object metadata, so re-running a batch with the same file is a no-op.
"""
import csv
import hashlib
import io
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def file_metadata(path: Path, row_count: int) -> dict:
    data = path.read_bytes()
    return {"file_name": path.name, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "row_count": row_count}


def _s3(settings):
    import boto3
    return boto3.client("s3", region_name=settings.aws_region)


def upload_bronze(settings, batch_id: str, path: Path, sha256: str, retries: int = 3):
    """Upload one raw file to bronze/batch=<id>/<file>. Returns the S3 key,
    or None when no bucket is configured (local run)."""
    if not settings.s3_bucket:
        return None
    s3, key = _s3(settings), f"bronze/batch={batch_id}/{path.name}"
    try:
        head = s3.head_object(Bucket=settings.s3_bucket, Key=key)
        if head.get("Metadata", {}).get("sha256") == sha256:
            log.info("bronze: %s already landed (same checksum), skipped", key)
            return key
    except Exception:
        pass                               # not there yet
    for attempt in range(1, retries + 1):
        try:
            s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=path.read_bytes(),
                          Metadata={"sha256": sha256, "batch_id": batch_id})
            log.info("bronze: landed s3://%s/%s", settings.s3_bucket, key)
            return key
        except Exception:
            if attempt == retries:
                raise
            log.warning("bronze: upload attempt %d failed for %s, retrying", attempt, key)


def upload_quarantine(settings, batch_id: str, entity: str, quarantine: list[dict]):
    """Write the batch's rejected records for one entity to quarantine/ as CSV."""
    if not settings.s3_bucket or not quarantine:
        return None
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["record_key", "reason_codes", "raw_record"])
    writer.writeheader()
    writer.writerows(quarantine)
    key = f"quarantine/batch={batch_id}/{entity}_rejected.csv"
    _s3(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=buffer.getvalue().encode())
    return key
