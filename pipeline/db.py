"""Database connection and schema setup.

The password is fetched from SSM Parameter Store at run time using the EC2
instance's IAM role, so no password is ever stored in code or config files.
For local development only, RB_DB_PASSWORD can be set instead.

Usage:  python -m pipeline.db apply-schema
"""
import os
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

from pipeline.config import REPO_ROOT, Settings, get_settings

SCHEMA_FILE = REPO_ROOT / "sql" / "schema.sql"
VIEWS_FILE = REPO_ROOT / "sql" / "kpi_views.sql"


def get_db_password(settings: Settings) -> str:
    local = os.environ.get("RB_DB_PASSWORD")
    if local:                      # local development only
        return local
    import boto3                   # imported here so tests don't need AWS
    ssm = boto3.client("ssm", region_name=settings.aws_region)
    response = ssm.get_parameter(Name=settings.db_password_param, WithDecryption=True)
    return response["Parameter"]["Value"]


def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = URL.create(
        "mysql+pymysql",
        username=settings.db_user,
        password=get_db_password(settings),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )
    connect_args = {"ssl": {"ca": settings.db_ssl_ca}} if settings.db_ssl_ca else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


def split_sql(sql: str) -> list[str]:
    """Split a .sql file into statements: drop '--' comments, then split on a
    ';' that ends a line (so a ';' inside a text value is left alone)."""
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in re.split(r";[ \t]*(?:\n|$)", without_comments) if s.strip()]


def run_sql_file(engine: Engine, path: Path) -> int:
    statements = split_sql(path.read_text(encoding="utf-8"))
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    return len(statements)


def apply_schema(engine: Engine) -> int:
    """Create tables, then (re)create the KPI views. Safe to run repeatedly."""
    return run_sql_file(engine, SCHEMA_FILE) + run_sql_file(engine, VIEWS_FILE)


if __name__ == "__main__":
    if sys.argv[1:] != ["apply-schema"]:
        sys.exit("usage: python -m pipeline.db apply-schema")
    count = apply_schema(get_engine())
    print(f"schema applied: {count} statements")
