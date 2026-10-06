"""Copy the official Day 1 / Day 2 source files into the S3 landing zone
(landing/batch=<id>/<entity>/<file>), as the bank's upstream systems would.
Skips files already there with the same content, so it is safe to re-run.
Usage:  python -m scripts.seed_landing
"""
import hashlib

from pipeline.config import BATCH_FILES, get_settings, raw_path
from pipeline.sources import _s3, landing_prefix, upload_landing


def main():
    settings = get_settings()
    if not settings.s3_bucket:
        raise SystemExit("RB_S3_BUCKET is not set; nothing to seed")
    s3 = _s3(settings)
    for batch_id, files in BATCH_FILES.items():
        for entity in files:
            path = raw_path(settings, batch_id, entity)
            data = path.read_bytes()
            key = f"{landing_prefix(batch_id)}{entity}/{path.name}"
            try:
                head = s3.head_object(Bucket=settings.s3_bucket, Key=key)
                if head["ContentLength"] == len(data) and head["ETag"].strip('"') == hashlib.md5(data).hexdigest():
                    print(f"already in landing: {key}")
                    continue
            except Exception:
                pass
            print("landed:", upload_landing(settings, batch_id, entity, path.name, data))


if __name__ == "__main__":
    main()
