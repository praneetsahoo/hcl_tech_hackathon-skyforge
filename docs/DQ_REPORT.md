# Data-quality report

Generated from the live database by `python -m scripts.dq_report`. Every file balances: **received = passed + rejected + duplicates removed**. Rejected rows are in the `dq_quarantine` table and in S3 `quarantine/`, each with the original record and all reason codes.

## Scorecard (KPI 10)

| Batch | File | Received | Passed | Rejected | Duplicates | Pass rate | Top reject reasons |
| --- | --- | --- | --- | --- | --- | --- | --- |
| day1 | branches.csv | 102 | 99 | 0 | 3 | 97.1% | – |
| day1 | customers.csv | 257 | 246 | 5 | 6 | 95.7% | MISSING_ACCOUNT_ID (4), SHARED_ACCOUNT_ID (1) |
| day1 | products.json | 114 | 111 | 0 | 3 | 97.4% | – |
| day1 | transactions.csv | 940 | 739 | 179 | 22 | 78.6% | ORPHAN_PRODUCT (39), CONFLICTING_DUPLICATE (36), ORPHAN_BRANCH (31) |
| day2 | branches_day2.csv | 2 | 2 | 0 | 0 | 100.0% | – |
| day2 | customer_updates_day2.csv | 47 | 47 | 0 | 0 | 100.0% | – |
| day2 | products_day2.json | 18 | 18 | 0 | 0 | 100.0% | – |
| day2 | transactions_day2.csv | 199 | 152 | 43 | 4 | 76.4% | ORPHAN_PRODUCT (11), ORPHAN_BRANCH (10), CONFLICTING_DUPLICATE (6) |

## What was found and how it was handled

REJECT = quarantined; FLAG = fixed or marked, and the record was kept.

| Batch | File | Type | Issue | Records | Handling |
| --- | --- | --- | --- | --- | --- |
| day1 | branches.csv | FLAG | `MISSING_MANAGER` | 4 | Kept: manager set to UNKNOWN |
| day1 | branches.csv | FLAG | `NEAR_DUPLICATE_RESOLVED` | 3 | Fixed: same ID re-sent with spacing/casing/value differences; best-quality copy kept |
| day1 | branches.csv | FLAG | `INVALID_CONTACT` | 1 | Kept: contact number flagged |
| day1 | branches.csv | FLAG | `AMBIGUOUS_DATE` | 1 | Kept: dd/mm vs mm/dd ambiguous; read day-first (Indian format) |
| day1 | customers.csv | REJECT | `MISSING_ACCOUNT_ID` | 4 | Rejected: a customer must own an account to link transactions |
| day1 | customers.csv | REJECT | `SHARED_ACCOUNT_ID` | 1 | Rejected: earlier-registered customer keeps the account |
| day1 | customers.csv | FLAG | `INVALID_EMAIL` | 9 | Kept: email_valid = 0 |
| day1 | customers.csv | FLAG | `INVALID_DOB` | 6 | Kept: future or implausible date of birth cleared to unknown |
| day1 | customers.csv | FLAG | `NEAR_DUPLICATE_RESOLVED` | 6 | Fixed: same ID re-sent with spacing/casing/value differences; best-quality copy kept |
| day1 | customers.csv | FLAG | `MISSING_DOB` | 5 | Kept: date of birth unknown |
| day1 | customers.csv | FLAG | `MISSING_EMAIL` | 3 | Kept: email empty |
| day1 | customers.csv | FLAG | `INVALID_PHONE` | 3 | Kept: phone_valid = 0 |
| day1 | customers.csv | FLAG | `AMBIGUOUS_DATE` | 3 | Kept: dd/mm vs mm/dd ambiguous; read day-first (Indian format) |
| day1 | customers.csv | FLAG | `MISSING_GENDER` | 1 | Kept: gender set to Unknown |
| day1 | products.json | FLAG | `MISSING_IS_ACTIVE` | 3 | Fixed: missing is_active treated as inactive, flagged |
| day1 | products.json | FLAG | `NEGATIVE_PRICE_CORRECTED` | 3 | Fixed: negative price is a sign error (mirrors a real product); made positive |
| day1 | products.json | FLAG | `NEAR_DUPLICATE_RESOLVED` | 3 | Fixed: same ID re-sent with spacing/casing/value differences; best-quality copy kept |
| day1 | products.json | FLAG | `MISSING_PRODUCT_NAME` | 2 | Kept: product name empty |
| day1 | products.json | FLAG | `MALFORMED_JSON_SALVAGED` | 1 | Fixed: products.json missing a comma; every object recovered |
| day1 | transactions.csv | REJECT | `ORPHAN_PRODUCT` | 39 | Rejected: product not in the product dimension |
| day1 | transactions.csv | REJECT | `CONFLICTING_DUPLICATE` | 36 | Rejected: same transaction_id with different values; we don't guess money |
| day1 | transactions.csv | REJECT | `ORPHAN_BRANCH` | 31 | Rejected: branch not in the branch dimension |
| day1 | transactions.csv | REJECT | `ORPHAN_ACCOUNT` | 29 | Rejected: account not in the customer dimension |
| day1 | transactions.csv | REJECT | `MISSING_AMOUNT` | 20 | Rejected: blank amount |
| day1 | transactions.csv | REJECT | `NEGATIVE_AMOUNT` | 15 | Rejected: negative amount on a non-refund |
| day1 | transactions.csv | REJECT | `ZERO_AMOUNT` | 14 | Rejected: amount is 0 |
| day1 | transactions.csv | FLAG | `DATE_MISMATCH` | 252 | Kept: `date` column disagrees with `timestamp`; timestamp is the truth, row flagged |
| day1 | transactions.csv | FLAG | `MISSING_REFUND_FLAG` | 135 | Kept: blank is_refund treated as not a refund, flagged |
| day1 | transactions.csv | FLAG | `EXACT_DUPLICATE` | 22 | Removed: byte-identical re-sent row (one copy kept) |
| day1 | transactions.csv | FLAG | `NON_INR_CURRENCY` | 21 | Kept: loaded with needs_review = 1, excluded from rupee KPIs |
| day2 | customer_updates_day2.csv | FLAG | `MISSING_EMAIL` | 2 | Kept: email empty |
| day2 | customer_updates_day2.csv | FLAG | `INVALID_EMAIL` | 1 | Kept: email_valid = 0 |
| day2 | customer_updates_day2.csv | FLAG | `MISSING_GENDER` | 1 | Kept: gender set to Unknown |
| day2 | products_day2.json | FLAG | `MISSING_PRODUCT_NAME` | 1 | Kept: product name empty |
| day2 | transactions_day2.csv | REJECT | `ORPHAN_PRODUCT` | 11 | Rejected: product not in the product dimension |
| day2 | transactions_day2.csv | REJECT | `ORPHAN_BRANCH` | 10 | Rejected: branch not in the branch dimension |
| day2 | transactions_day2.csv | REJECT | `CONFLICTING_DUPLICATE` | 6 | Rejected: same transaction_id with different values; we don't guess money |
| day2 | transactions_day2.csv | REJECT | `ORPHAN_ACCOUNT` | 6 | Rejected: account not in the customer dimension |
| day2 | transactions_day2.csv | REJECT | `ZERO_AMOUNT` | 5 | Rejected: amount is 0 |
| day2 | transactions_day2.csv | REJECT | `MISSING_AMOUNT` | 3 | Rejected: blank amount |
| day2 | transactions_day2.csv | REJECT | `NEGATIVE_AMOUNT` | 3 | Rejected: negative amount on a non-refund |
| day2 | transactions_day2.csv | FLAG | `DATE_MISMATCH` | 44 | Kept: `date` column disagrees with `timestamp`; timestamp is the truth, row flagged |
| day2 | transactions_day2.csv | FLAG | `MISSING_REFUND_FLAG` | 19 | Kept: blank is_refund treated as not a refund, flagged |
| day2 | transactions_day2.csv | FLAG | `EXACT_DUPLICATE` | 4 | Removed: byte-identical re-sent row (one copy kept) |
| day2 | transactions_day2.csv | FLAG | `NON_INR_CURRENCY` | 4 | Kept: loaded with needs_review = 1, excluded from rupee KPIs |

Note: one rejected record can carry several reasons, so reason counts can add up to more than the rejected total.
