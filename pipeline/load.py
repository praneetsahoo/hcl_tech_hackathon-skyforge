"""Gold layer: load clean records into the star schema.

All functions take an open SQLAlchemy connection that is INSIDE one
transaction (opened by run_pipeline). If anything fails, the caller rolls
back and nothing from the batch is half-loaded.
"""
import hashlib
import json
from collections import defaultdict
from datetime import date

from sqlalchemy import text

FIRST_VALID_FROM = date(1900, 1, 1)   # first version of a key: "known since always"
OPEN_VALID_TO = date(9999, 12, 31)

# table, business key, surrogate key, all attribute columns, SCD2-tracked columns
DIMENSIONS = {
    "customers": ("dim_customer", "customer_id", "customer_sk",
                  ["customer_name", "email", "email_valid", "phone", "phone_valid", "account_id",
                   "gender", "dob", "address", "kyc_status", "registration_date"],
                  ["kyc_status", "address", "email", "phone", "account_id"]),
    "products": ("dim_product", "product_id", "product_sk",
                 ["product_name", "product_type", "category", "price", "launch_date",
                  "is_active", "vendor_name"],
                 ["price", "is_active", "product_type", "category"]),
    "branches": ("dim_branch", "branch_id", "branch_sk",
                 ["branch_name", "location", "manager_name", "opened_date", "region",
                  "branch_type", "contact_number"],
                 ["manager_name", "region", "branch_type"]),
}

FACT_COLUMNS = ["account_id", "customer_id", "product_id", "branch_id", "txn_ts", "txn_date",
                "amount", "currency", "amount_inr", "payment_method", "status", "is_refund",
                "remarks", "date_mismatch", "needs_review"]


def _norm(value):
    """Make DB values and freshly cleaned values compare equal (dates, decimals, None)."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ") if hasattr(value, "hour") else value.isoformat()
    if hasattr(value, "quantize"):
        return f"{value:.2f}"
    return str(value)


def row_hash(record: dict, columns: list[str]) -> str:
    payload = json.dumps([_norm(record.get(c)) for c in columns])
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Dimensions: SCD Type 2 merge
# ---------------------------------------------------------------------------

def merge_dimension(conn, entity: str, records: list[dict], batch_id: str, batch_date: date) -> dict:
    """Insert new keys, version business-meaningful changes (SCD2), fix
    minor changes in place (SCD1), and skip unchanged rows (idempotent)."""
    table, key, _sk, attrs, tracked = DIMENSIONS[entity]
    current = {r[key]: dict(r) for r in conn.execute(
        text(f"SELECT * FROM {table} WHERE is_current = 1")).mappings()}
    counts = {"inserted": 0, "new_version": 0, "updated_in_place": 0, "unchanged": 0}
    insert_sql = text(
        f"INSERT INTO {table} ({key}, {', '.join(attrs)}, valid_from, valid_to, is_current, row_hash, batch_id) "
        f"VALUES (:{key}, {', '.join(':' + a for a in attrs)}, :valid_from, :valid_to, 1, :row_hash, :batch_id)")

    for record in records:
        new_hash = row_hash(record, attrs)
        old = current.get(record[key])
        params = {**record, "row_hash": new_hash, "batch_id": batch_id, "valid_to": OPEN_VALID_TO}
        if old is None:
            conn.execute(insert_sql, {**params, "valid_from": FIRST_VALID_FROM})
            counts["inserted"] += 1
        elif old["row_hash"] == new_hash:
            counts["unchanged"] += 1
        elif any(_norm(old[c]) != _norm(record[c]) for c in tracked):
            conn.execute(text(f"UPDATE {table} SET is_current = 0, valid_to = :d WHERE {key} = :k AND is_current = 1"),
                         {"d": batch_date, "k": record[key]})
            conn.execute(insert_sql, {**params, "valid_from": batch_date})
            counts["new_version"] += 1
        else:
            sets = ", ".join(f"{a} = :{a}" for a in attrs)
            conn.execute(text(f"UPDATE {table} SET {sets}, row_hash = :row_hash, batch_id = :batch_id "
                              f"WHERE {key} = :{key} AND is_current = 1"), params)
            counts["updated_in_place"] += 1
    return counts


def dimension_keys(conn) -> tuple[set, set, set]:
    """Known accounts, products and branches (any version) for referential checks."""
    q = lambda sql: {r[0] for r in conn.execute(text(sql))}
    return (q("SELECT DISTINCT account_id FROM dim_customer"),
            q("SELECT DISTINCT product_id FROM dim_product"),
            q("SELECT DISTINCT branch_id FROM dim_branch"))


def _versions(conn, table, key_col, sk_col, extra=""):
    out = defaultdict(list)
    for row in conn.execute(text(f"SELECT {key_col}, {sk_col}, valid_from, valid_to {extra} FROM {table}")):
        out[row[0]].append(row)
    return out


def _point_in_time(versions, key, day):
    """The version of `key` that was valid on `day` (valid_from <= day < valid_to)."""
    for row in versions.get(key, []):
        if row[2] <= day < row[3]:
            return row
    return None


# ---------------------------------------------------------------------------
# Fact: insert new, correct changed, skip identical
# ---------------------------------------------------------------------------

def load_facts(conn, records: list[dict], batch_id: str, run_id: str) -> dict:
    customers = _versions(conn, "dim_customer", "account_id", "customer_sk", ", customer_id")
    products = _versions(conn, "dim_product", "product_id", "product_sk")
    branches = _versions(conn, "dim_branch", "branch_id", "branch_sk")
    existing = {r["transaction_id"]: dict(r) for r in conn.execute(
        text(f"SELECT transaction_id, row_hash, {', '.join(FACT_COLUMNS)} FROM fact_transaction")).mappings()}

    counts = {"inserted": 0, "corrected": 0, "unchanged": 0}
    to_insert = []
    for record in records:
        day = record["txn_date"]
        cust = _point_in_time(customers, record["account_id"], day)
        prod = _point_in_time(products, record["product_id"], day)
        br = _point_in_time(branches, record["branch_id"], day)
        if not (cust and prod and br):
            raise ValueError(f"no dimension version for {record['transaction_id']} on {day}")
        row = {**record, "customer_id": cust[4], "customer_sk": cust[1],
               "product_sk": prod[1], "branch_sk": br[1]}
        row["row_hash"] = row_hash(row, FACT_COLUMNS)
        old = existing.get(row["transaction_id"])
        if old is None:
            to_insert.append({**row, "source_batch_id": batch_id, "last_updated_batch_id": batch_id})
            counts["inserted"] += 1
        elif old["row_hash"] == row["row_hash"]:
            counts["unchanged"] += 1
        else:
            changed = [c for c in FACT_COLUMNS if _norm(old[c]) != _norm(row[c])]
            conn.execute(text(
                "INSERT INTO fact_correction_log (transaction_id, batch_id, run_id, old_values, new_values) "
                "VALUES (:t, :b, :r, :old, :new)"),
                {"t": row["transaction_id"], "b": batch_id, "r": run_id,
                 "old": json.dumps({c: _norm(old[c]) for c in changed}),
                 "new": json.dumps({c: _norm(row[c]) for c in changed})})
            sets = ", ".join(f"{c} = :{c}" for c in FACT_COLUMNS + ["customer_sk", "product_sk", "branch_sk", "row_hash"])
            conn.execute(text(f"UPDATE fact_transaction SET {sets}, last_updated_batch_id = :b "
                              f"WHERE transaction_id = :transaction_id"), {**row, "b": batch_id})
            counts["corrected"] += 1

    if to_insert:
        cols = ["transaction_id", "customer_sk", "product_sk", "branch_sk", *FACT_COLUMNS,
                "source_batch_id", "last_updated_batch_id", "row_hash"]
        conn.execute(text(f"INSERT INTO fact_transaction ({', '.join(cols)}) "
                          f"VALUES ({', '.join(':' + c for c in cols)})"), to_insert)
    return counts


# ---------------------------------------------------------------------------
# Data-quality outputs (replaced for the batch on every run -> idempotent)
# ---------------------------------------------------------------------------

def write_dq(conn, batch_id: str, run_id: str, results: list, ingestion: list[dict]):
    for table in ("dq_quarantine", "dq_scorecard", "dq_issue_counts", "ingestion_log"):
        conn.execute(text(f"DELETE FROM {table} WHERE batch_id = :b"), {"b": batch_id})

    conn.execute(text(
        "INSERT INTO ingestion_log (batch_id, file_name, entity, row_count, size_bytes, sha256, s3_key) "
        "VALUES (:batch_id, :file_name, :entity, :row_count, :size_bytes, :sha256, :s3_key)"), ingestion)

    quarantine = [{**q, "run_id": run_id, "batch_id": batch_id, "entity": r.entity, "source_file": r.source_file}
                  for r in results for q in r.quarantine]
    if quarantine:
        conn.execute(text(
            "INSERT INTO dq_quarantine (run_id, batch_id, entity, source_file, record_key, reason_codes, raw_record) "
            "VALUES (:run_id, :batch_id, :entity, :source_file, :record_key, :reason_codes, :raw_record)"), quarantine)

    conn.execute(text(
        "INSERT INTO dq_scorecard VALUES (:b, :f, :e, :recv, :passed, :rej, :dup, :run)"),
        [{"b": batch_id, "f": r.source_file, "e": r.entity, "recv": r.received, "passed": r.passed,
          "rej": len(r.quarantine), "dup": r.duplicates, "run": run_id} for r in results])

    issues = [{"b": batch_id, "f": r.source_file, "s": sev, "c": code, "n": n}
              for r in results for (sev, code), n in r.issues.items()]
    if issues:
        conn.execute(text("INSERT INTO dq_issue_counts VALUES (:b, :f, :s, :c, :n)"), issues)
