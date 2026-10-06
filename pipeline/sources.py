"""Landing zone: where a batch's source files come from.

Upstream systems (or the dashboard's "Upload & run" tab, which plays that
role) drop files into the S3 landing zone:

    s3://<bucket>/landing/batch=<batch_id>/<entity>/<original file name>

The pipeline then lands them, unchanged, in bronze and processes them.
Day 1 and Day 2 have a fixed manifest (all four files required); any later
batch (day3, ...) may contain only the entities that changed: a missing
file simply means "nothing new for that entity".

Without an S3 bucket (local development) the same layout is used under
data/landing/, and Day 1 / Day 2 fall back to data/raw/<batch_id>/.
"""
import logging
import re
import shutil
import tempfile
from pathlib import Path

from pipeline.config import BATCH_FILES, ENTITIES, ENTITY_EXTENSION, raw_path, validate_batch_id

log = logging.getLogger(__name__)


def landing_prefix(batch_id: str) -> str:
    return f"landing/batch={batch_id}/"


def safe_file_name(name: str, entity: str) -> str:
    """Keep only the base name, allow letters/digits/._- and require the right extension."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name or "").name)
    if not base or not base.lower().endswith(ENTITY_EXTENSION[entity]):
        raise ValueError(f"{entity} file must be a {ENTITY_EXTENSION[entity]} file (got {name!r})")
    return base


def _s3(settings):
    import boto3
    return boto3.client("s3", region_name=settings.aws_region)


def upload_landing(settings, batch_id: str, entity: str, file_name: str, data: bytes) -> str:
    """Drop one source file into the landing zone (what an upstream system does)."""
    validate_batch_id(batch_id)
    if entity not in ENTITIES:
        raise ValueError(f"unknown entity {entity!r}")
    if not data:
        raise ValueError(f"{file_name} is empty")
    name = safe_file_name(file_name, entity)
    if settings.s3_bucket:
        key = f"{landing_prefix(batch_id)}{entity}/{name}"
        _s3(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=data)
        return f"s3://{settings.s3_bucket}/{key}"
    folder = settings.data_dir / "landing" / batch_id / entity
    if folder.exists():
        shutil.rmtree(folder)                    # one current file per entity
    folder.mkdir(parents=True)
    (folder / name).write_bytes(data)
    return str(folder / name)


def _download_landing(settings, batch_id: str, work_dir: Path) -> dict:
    """Copy this batch's landing files from S3 to work_dir. Newest file wins per entity."""
    s3 = _s3(settings)
    newest = {}
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket,
                                                         Prefix=landing_prefix(batch_id))
    for page in pages:
        for obj in page.get("Contents", []):
            parts = obj["Key"][len(landing_prefix(batch_id)):].split("/")
            if len(parts) != 2 or parts[0] not in ENTITIES or not parts[1]:
                continue
            entity = parts[0]
            if entity not in newest or obj["LastModified"] > newest[entity]["LastModified"]:
                newest[entity] = obj
    files = {}
    for entity, obj in newest.items():
        target = work_dir / entity / obj["Key"].rsplit("/", 1)[1]
        target.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(settings.s3_bucket, obj["Key"], str(target))
        files[entity] = target
    return files


def resolve_sources(settings, batch_id: str) -> tuple[dict, str, Path | None]:
    """Find the batch's source files. Returns ({entity: path}, origin, temp_dir_to_clean)."""
    validate_batch_id(batch_id)
    if settings.s3_bucket:
        work_dir = Path(tempfile.mkdtemp(prefix=f"rb_{batch_id}_"))
        files = _download_landing(settings, batch_id, work_dir)
        if files:
            _check_manifest(batch_id, files)
            return files, f"s3://{settings.s3_bucket}/{landing_prefix(batch_id)}", work_dir
        shutil.rmtree(work_dir, ignore_errors=True)

    if batch_id in BATCH_FILES:                  # official Day 1 / Day 2 drops in the repo
        files = {e: raw_path(settings, batch_id, e) for e in BATCH_FILES[batch_id]}
        return files, f"{settings.data_dir / 'raw' / batch_id}", None

    local = settings.data_dir / "landing" / batch_id
    files = {}
    for entity in ENTITIES:
        found = sorted((local / entity).glob("*")) if (local / entity).is_dir() else []
        if found:
            files[entity] = found[-1]
    if files:
        return files, str(local), None
    raise FileNotFoundError(f"no source files found for batch {batch_id!r} in the landing zone")


def _check_manifest(batch_id: str, files: dict):
    """Day 1 / Day 2 must arrive complete; later batches may be partial."""
    if batch_id in BATCH_FILES:
        missing = sorted(set(BATCH_FILES[batch_id]) - set(files))
        if missing:
            raise FileNotFoundError(f"source file missing or empty: {', '.join(missing)} for {batch_id}")
