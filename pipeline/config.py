"""Central settings for the RetailBank pipeline.

Everything comes from environment variables so the same code runs on a
laptop and on EC2. No passwords live here: on AWS the database password is
read from SSM Parameter Store at run time (see DB_PASSWORD_PARAM).
Settings are read when get_settings() is called, not at import time, so
tests can override them.
"""
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Which source file holds which entity, per batch. The pipeline is always
# run for one batch_id, and only that batch's files are read.
BATCH_FILES = {
    "day1": {
        "branches": "branches.csv",
        "customers": "customers.csv",
        "products": "products.json",
        "transactions": "transactions.csv",
    },
    "day2": {
        "branches": "branches_day2.csv",
        "customers": "customer_updates_day2.csv",
        "products": "products_day2.json",
        "transactions": "transactions_day2.csv",
    },
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path          # local folder with raw/<batch_id>/ files
    aws_region: str
    s3_bucket: str          # bronze/quarantine bucket ("" = local only)
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password_param: str  # SSM parameter name, never the password itself
    log_file: str           # "" = log to console only


def get_settings() -> Settings:
    env = os.environ
    return Settings(
        data_dir=Path(env.get("RB_DATA_DIR", REPO_ROOT / "data")),
        aws_region=env.get("AWS_REGION", "ap-southeast-2"),
        s3_bucket=env.get("RB_S3_BUCKET", ""),
        db_host=env.get("RB_DB_HOST", "localhost"),
        db_port=int(env.get("RB_DB_PORT", "3306")),
        db_name=env.get("RB_DB_NAME", "retailbank"),
        db_user=env.get("RB_DB_USER", "retailbank_app"),
        db_password_param=env.get("RB_DB_PASSWORD_PARAM", "/retailbank/db/app_password"),
        log_file=env.get("RB_LOG_FILE", ""),
    )


def raw_path(settings: Settings, batch_id: str, entity: str) -> Path:
    """Path of one source file for a batch, e.g. data/raw/day1/customers.csv."""
    if batch_id not in BATCH_FILES:
        raise ValueError(f"Unknown batch_id {batch_id!r}; expected one of {sorted(BATCH_FILES)}")
    return settings.data_dir / "raw" / batch_id / BATCH_FILES[batch_id][entity]
