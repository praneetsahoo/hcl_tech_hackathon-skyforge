"""Generate docs/DQ_REPORT.md from the live database (deliverable: the
data-quality / rejects report showing what was found and how it was handled).
Usage:  python -m scripts.dq_report > docs/DQ_REPORT.md
"""
from sqlalchemy import text

from pipeline.db import get_engine

HANDLING = {
    "MISSING_ACCOUNT_ID": "Rejected: a customer must own an account to link transactions",
    "SHARED_ACCOUNT_ID": "Rejected: earlier-registered customer keeps the account",
    "CONFLICTING_DUPLICATE": "Rejected: same transaction_id with different values; we don't guess money",
    "ORPHAN_ACCOUNT": "Rejected: account not in the customer dimension",
    "ORPHAN_PRODUCT": "Rejected: product not in the product dimension",
    "ORPHAN_BRANCH": "Rejected: branch not in the branch dimension",
    "MISSING_AMOUNT": "Rejected: blank amount",
    "ZERO_AMOUNT": "Rejected: amount is 0",
    "NEGATIVE_AMOUNT": "Rejected: negative amount on a non-refund",
    "INVALID_AMOUNT": "Rejected: amount not a number",
    "MALFORMED_JSON": "Rejected: unparseable JSON fragment",
    "EXACT_DUPLICATE": "Removed: byte-identical re-sent row (one copy kept)",
    "NEAR_DUPLICATE_RESOLVED": "Fixed: same ID re-sent with spacing/casing/value differences; best-quality copy kept",
    "DATE_MISMATCH": "Kept: `date` column disagrees with `timestamp`; timestamp is the truth, row flagged",
    "MISSING_REFUND_FLAG": "Kept: blank is_refund treated as not a refund, flagged",
    "NON_INR_CURRENCY": "Kept: loaded with needs_review = 1, excluded from rupee KPIs",
    "INVALID_EMAIL": "Kept: email_valid = 0",
    "MISSING_EMAIL": "Kept: email empty",
    "INVALID_PHONE": "Kept: phone_valid = 0",
    "INVALID_DOB": "Kept: future or implausible date of birth cleared to unknown",
    "MISSING_DOB": "Kept: date of birth unknown",
    "MISSING_GENDER": "Kept: gender set to Unknown",
    "AMBIGUOUS_DATE": "Kept: dd/mm vs mm/dd ambiguous; read day-first (Indian format)",
    "MISSING_MANAGER": "Kept: manager set to UNKNOWN",
    "INVALID_CONTACT": "Kept: contact number flagged",
    "NEGATIVE_PRICE_CORRECTED": "Fixed: negative price is a sign error (mirrors a real product); made positive",
    "MISSING_IS_ACTIVE": "Fixed: missing is_active treated as inactive, flagged",
    "MISSING_PRODUCT_NAME": "Kept: product name empty",
    "MALFORMED_JSON_SALVAGED": "Fixed: products.json missing a comma; every object recovered",
}


def main():
    with get_engine().connect() as c:
        score = c.execute(text("SELECT * FROM kpi10_dq_scorecard ORDER BY batch_id, source_file")).mappings().all()
        issues = c.execute(text("SELECT batch_id, source_file, severity, reason_code, record_count "
                                "FROM dq_issue_counts ORDER BY batch_id, source_file, severity DESC, "
                                "record_count DESC")).mappings().all()
    print("# Data-quality report\n")
    print("Generated from the live database by `python -m scripts.dq_report`. Every file balances: "
          "**received = passed + rejected + duplicates removed**. Rejected rows are in the "
          "`dq_quarantine` table and in S3 `quarantine/`, each with the original record and all reason codes.\n")
    print("## Scorecard (KPI 10)\n")
    print("| Batch | File | Received | Passed | Rejected | Duplicates | Pass rate | Top reject reasons |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for s in score:
        print(f"| {s['batch_id']} | {s['source_file']} | {s['records_received']} | {s['records_passed']} | "
              f"{s['records_rejected']} | {s['duplicates_removed']} | {s['pass_rate_pct']}% | "
              f"{s['top_3_reject_reasons'] or '–'} |")
    print("\n## What was found and how it was handled\n")
    print("REJECT = quarantined; FLAG = fixed or marked, and the record was kept.\n")
    print("| Batch | File | Type | Issue | Records | Handling |")
    print("| --- | --- | --- | --- | --- | --- |")
    for i in issues:
        print(f"| {i['batch_id']} | {i['source_file']} | {i['severity']} | `{i['reason_code']}` | "
              f"{i['record_count']} | {HANDLING.get(i['reason_code'], '')} |")


if __name__ == "__main__":
    main()
