"""Silver layer: validate and standardise each source record.

Every record ends in exactly one bucket:
  passed     -> clean record, goes on to the gold tables
  rejected   -> quarantine, with the original raw record and ALL reason codes
  duplicate  -> an extra copy of a record we already kept (counted, not loaded)
so for every file:  received = passed + rejected + duplicates.

Rules return two kinds of issues:
  REJECT  -> the record cannot be trusted, it is quarantined
  FLAG    -> the value was fixed or marked, the record is kept
"""
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from pipeline import rules

ID_FIELD = {"customers": "customer_id", "products": "product_id",
            "branches": "branch_id", "transactions": "transaction_id"}


@dataclass
class EntityResult:
    entity: str
    source_file: str
    received: int = 0
    clean: list = field(default_factory=list)        # list of dicts
    quarantine: list = field(default_factory=list)   # list of dicts
    duplicates: int = 0
    issues: Counter = field(default_factory=Counter) # (severity, code) -> count
    raw_by_key: dict = field(default_factory=dict)   # id -> original raw record

    def reject(self, raw: dict, reasons: list[str]):
        self.quarantine.append({
            "record_key": rules.clean_text(raw.get(ID_FIELD[self.entity], ""))[:50] or None,
            "reason_codes": ";".join(reasons),
            "raw_record": json.dumps(raw, default=str, ensure_ascii=False),
        })
        for code in reasons:
            self.issues[("REJECT", code)] += 1

    def flag(self, codes: list[str]):
        for code in codes:
            self.issues[("FLAG", code)] += 1

    @property
    def passed(self) -> int:
        return len(self.clean)


# ---------------------------------------------------------------------------
# Per-record cleaners: raw dict -> (clean dict or None, rejects, flags)
# ---------------------------------------------------------------------------

def clean_customer(raw: dict, batch_date: date):
    rejects, flags = [], []
    customer_id = rules.clean_text(raw.get("customer_id"))
    account_id = rules.clean_text(raw.get("account_id")).upper()
    if not customer_id:
        rejects.append("MISSING_CUSTOMER_ID")
    if not account_id:
        rejects.append("MISSING_ACCOUNT_ID")
    kyc = rules.lookup(raw.get("kyc_status"), rules.KYC)
    if kyc is None:
        rejects.append("INVALID_KYC_STATUS")

    email, email_valid = rules.clean_email(raw.get("email"))
    if email is None:
        flags.append("MISSING_EMAIL")
    elif not email_valid:
        flags.append("INVALID_EMAIL")
    phone, phone_valid = rules.clean_phone(raw.get("phone"))
    if not phone_valid:
        flags.append("INVALID_PHONE")

    gender = rules.lookup(raw.get("gender"), rules.GENDER)
    if gender is None:
        gender = "Unknown"
        flags.append("MISSING_GENDER")

    dob, _ = rules.parse_date(raw.get("dob"))
    if dob is None:
        flags.append("MISSING_DOB")
    elif dob > batch_date or (batch_date.year - dob.year) > 120:
        flags.append("INVALID_DOB")      # future or implausible: kept as unknown
        dob = None

    registration_date, ambiguous = rules.parse_date(raw.get("registration_date"))
    if ambiguous:
        flags.append("AMBIGUOUS_DATE")
    if registration_date is None:
        flags.append("INVALID_REGISTRATION_DATE")

    if rejects:
        return None, rejects, flags
    return {
        "customer_id": customer_id,
        "customer_name": rules.clean_text(raw.get("customer_name")),
        "email": email, "email_valid": int(email_valid),
        "phone": phone, "phone_valid": int(phone_valid),
        "account_id": account_id,
        "gender": gender,
        "dob": dob,
        "address": rules.title_address(raw.get("address")) or None,
        "kyc_status": kyc,
        "registration_date": registration_date,
    }, rejects, flags


def clean_product(raw: dict, batch_date: date):
    rejects, flags = [], []
    product_id = rules.clean_text(raw.get("product_id"))
    if not product_id:
        rejects.append("MISSING_PRODUCT_ID")
    product_type = rules.lookup(raw.get("product_type"), rules.PRODUCT_TYPE)
    if product_type is None:
        rejects.append("INVALID_PRODUCT_TYPE")
    category = rules.lookup(raw.get("category"), rules.CATEGORY)
    if category is None:
        rejects.append("INVALID_CATEGORY")
    price = rules.parse_amount(raw.get("price"))
    if price is None:
        rejects.append("INVALID_PRICE")
    elif price < 0:
        price = -price                   # sign error: mirrors a real product's price
        flags.append("NEGATIVE_PRICE_CORRECTED")

    is_active = rules.parse_bool(raw.get("is_active"))
    if is_active is None:
        is_active = False
        flags.append("MISSING_IS_ACTIVE")
    name = rules.clean_text(raw.get("product_name"))
    if not name:
        flags.append("MISSING_PRODUCT_NAME")
    launch_date, _ = rules.parse_date(raw.get("launch_date"))

    if rejects:
        return None, rejects, flags
    return {
        "product_id": product_id, "product_name": name or None,
        "product_type": product_type, "category": category, "price": price,
        "launch_date": launch_date, "is_active": int(is_active),
        "vendor_name": rules.clean_text(raw.get("vendor_name")) or None,
    }, rejects, flags


def clean_branch(raw: dict, batch_date: date):
    rejects, flags = [], []
    branch_id = rules.clean_text(raw.get("branch_id"))
    if not branch_id:
        rejects.append("MISSING_BRANCH_ID")
    region = rules.lookup(raw.get("region"), rules.REGION)
    if region is None:
        rejects.append("INVALID_REGION")
    branch_type = rules.lookup(raw.get("branch_type"), rules.BRANCH_TYPE)
    if branch_type is None:
        rejects.append("INVALID_BRANCH_TYPE")
    manager = rules.clean_text(raw.get("manager_name"))
    if not manager:
        manager = "UNKNOWN"
        flags.append("MISSING_MANAGER")
    contact, contact_valid = rules.clean_phone(raw.get("contact_number"))
    if not contact_valid:
        flags.append("INVALID_CONTACT")
    opened_date, ambiguous = rules.parse_date(raw.get("opened_date"))
    if ambiguous:
        flags.append("AMBIGUOUS_DATE")

    if rejects:
        return None, rejects, flags
    return {
        "branch_id": branch_id,
        "branch_name": rules.clean_text(raw.get("branch_name")),
        "location": rules.clean_text(raw.get("location")).title(),
        "manager_name": manager, "opened_date": opened_date,
        "region": region, "branch_type": branch_type,
        "contact_number": contact,
    }, rejects, flags


def clean_transaction(raw: dict, batch_date: date):
    rejects, flags = [], []
    transaction_id = rules.clean_text(raw.get("transaction_id"))
    keys = {k: rules.clean_text(raw.get(k)).upper() for k in ("account_id", "product_id", "branch_id")}
    if not transaction_id or not all(keys.values()):
        rejects.append("MISSING_KEY")

    ts = rules.parse_timestamp(raw.get("timestamp"))
    date_mismatch = 0
    if ts is None:
        rejects.append("INVALID_TIMESTAMP")
    else:
        if ts.date() > batch_date:
            rejects.append("FUTURE_DATED")
        if ts.date() not in rules.date_candidates(raw.get("date")):
            date_mismatch = 1             # timestamp is the source of truth
            flags.append("DATE_MISMATCH")

    is_refund = rules.lookup(raw.get("is_refund"), rules.REFUND)
    if is_refund is None:
        if rules.clean_text(raw.get("is_refund")):
            rejects.append("INVALID_REFUND_FLAG")
        else:
            is_refund = False
            flags.append("MISSING_REFUND_FLAG")

    raw_amount = rules.clean_text(raw.get("amount"))
    amount = rules.parse_amount(raw_amount)
    if not raw_amount:
        rejects.append("MISSING_AMOUNT")
    elif amount is None:
        rejects.append("INVALID_AMOUNT")
    elif amount == 0:
        rejects.append("ZERO_AMOUNT")
    elif amount < 0:
        rejects.append("NEGATIVE_AMOUNT")

    status = rules.lookup(raw.get("status"), rules.STATUS)
    if status is None:
        rejects.append("INVALID_STATUS")
    payment = rules.lookup(raw.get("payment_method"), rules.PAYMENT)
    if payment is None:
        rejects.append("INVALID_PAYMENT_METHOD")
    currency = rules.clean_text(raw.get("currency")).upper()
    if len(currency) != 3:
        rejects.append("INVALID_CURRENCY")
    needs_review = 0
    if currency != "INR":
        needs_review = 1                  # kept, but excluded from rupee KPIs
        flags.append("NON_INR_CURRENCY")

    if rejects:
        return None, rejects, flags
    remarks = rules.clean_text(str(raw.get("remarks", "")).replace("[late correction]", ""))
    return {
        "transaction_id": transaction_id, **keys,
        "txn_ts": ts, "txn_date": ts.date(),
        "amount": amount, "currency": currency,
        "amount_inr": amount if currency == "INR" else None,
        "payment_method": payment, "status": status,
        "is_refund": int(is_refund), "remarks": remarks or None,
        "date_mismatch": date_mismatch, "needs_review": needs_review,
    }, rejects, flags


CLEANERS = {"customers": clean_customer, "products": clean_product,
            "branches": clean_branch, "transactions": clean_transaction}


# ---------------------------------------------------------------------------
# Whole-file processing
# ---------------------------------------------------------------------------

def _raw_key(raw: dict) -> str:
    return json.dumps(raw, sort_keys=True, default=str)


def process_entity(entity: str, source_file: str, rows: list[dict],
                   lost_fragments: list[str], batch_date: date) -> EntityResult:
    result = EntityResult(entity, source_file, received=len(rows) + len(lost_fragments))
    for fragment in lost_fragments:                       # unrecoverable JSON
        result.reject({"fragment": fragment}, ["MALFORMED_JSON"])

    # 1. exact duplicate rows (re-sent, byte-identical): keep the first copy
    seen, unique_rows = set(), []
    for raw in rows:
        key = _raw_key(raw)
        if key in seen:
            result.duplicates += 1
            result.flag(["EXACT_DUPLICATE"])
        else:
            seen.add(key)
            unique_rows.append(raw)

    # 2. transactions: same ID with DIFFERENT values -> quarantine every version
    if entity == "transactions":
        ids = Counter(rules.clean_text(r.get("transaction_id")) for r in unique_rows)
        conflicting = {i for i, n in ids.items() if n > 1}
        keep = []
        for raw in unique_rows:
            if rules.clean_text(raw.get("transaction_id")) in conflicting:
                result.reject(raw, ["CONFLICTING_DUPLICATE"])
            else:
                keep.append(raw)
        unique_rows = keep

    # 3. clean each record
    cleaned = []                                          # (record, flags, raw)
    for raw in unique_rows:
        record, rejects, flags = CLEANERS[entity](raw, batch_date)
        if rejects:
            result.reject(raw, rejects)
            result.flag(flags)
        else:
            cleaned.append((record, flags, raw))

    # 4. master data: several copies of one ID -> keep the best-quality copy
    #    (fewest flags; on a tie the later copy, i.e. the most recently sent)
    if entity != "transactions":
        id_field = ID_FIELD[entity]
        best = {}
        for position, (record, flags, raw) in enumerate(cleaned):
            key = record[id_field]
            score = (-len(flags), position)
            if key in best:
                result.duplicates += 1
                result.flag(["NEAR_DUPLICATE_RESOLVED"])
                if score < best[key][0]:
                    continue
            best[key] = (score, record, flags, raw)
        cleaned = [(r, f, raw) for _, r, f, raw in best.values()]

    # 5. customers: one account may belong to only one customer
    if entity == "customers":
        df = pd.DataFrame([r for r, _, _ in cleaned])
        if not df.empty:
            df["_order"] = df["registration_date"].fillna(date.max)
            owners = df.sort_values(["_order", "customer_id"]).drop_duplicates("account_id")
            owner_ids = set(owners["customer_id"])
            keep = []
            for record, flags, raw in cleaned:
                if record["customer_id"] in owner_ids:
                    keep.append((record, flags, raw))
                else:
                    result.reject(raw, ["SHARED_ACCOUNT_ID"])
            cleaned = keep

    for record, flags, raw in cleaned:
        result.flag(flags)
        result.clean.append(record)
        result.raw_by_key[record[ID_FIELD[entity]]] = raw
    return result


def apply_referential_checks(result: EntityResult, accounts: set, products: set, branches: set):
    """Move transactions whose account/product/branch is unknown to quarantine."""
    keep = []
    for record in result.clean:
        reasons = [code for code, ok in (
            ("ORPHAN_ACCOUNT", record["account_id"] in accounts),
            ("ORPHAN_PRODUCT", record["product_id"] in products),
            ("ORPHAN_BRANCH", record["branch_id"] in branches)) if not ok]
        if reasons:
            result.reject(result.raw_by_key[record["transaction_id"]], reasons)
        else:
            keep.append(record)
    result.clean = keep
