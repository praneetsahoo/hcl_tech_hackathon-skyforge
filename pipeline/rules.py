"""Small, pure cleaning rules (no database, no AWS) so each is easy to test.

Each function takes one raw text value and returns the standardised value,
or None when the value cannot be understood.
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# ---------- text ----------

def clean_text(value) -> str:
    """Trim and collapse inner whitespace; None -> ''."""
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def title_address(value: str) -> str:
    """'  544 park ave, delhi   ' -> '544 Park Ave, Delhi'."""
    return clean_text(value).title() if value else ""


# ---------- code sets (every spelling seen in the profile maps to one value) ----------

STATUS = {"success": "Success", "succes": "Success", "sucess": "Success",
          "failed": "Failed", "faild": "Failed", "pending": "Pending"}
PAYMENT = {"upi": "UPI", "cash": "Cash", "card": "Card", "netbanking": "NetBanking", "cheque": "Cheque"}
REFUND = {"true": True, "y": True, "1": True, "false": False, "n": False, "0": False}
GENDER = {"male": "Male", "m": "Male", "female": "Female", "f": "Female", "other": "Other", "unknown": "Unknown"}
KYC = {"verified": "Verified", "pending": "Pending", "rejected": "Rejected"}
REGION = {"north": "North", "south": "South", "east": "East", "west": "West"}
BRANCH_TYPE = {"urban": "Urban", "rural": "Rural"}
CATEGORY = {"retail": "Retail", "sme": "SME", "corporate": "Corporate"}
PRODUCT_TYPE = {"savings account": "Savings Account", "loan": "Loan",
                "credit card": "Credit Card", "fixed deposit": "Fixed Deposit"}
BOOL_TEXT = {"true": True, "false": False}


def lookup(value, mapping: dict):
    """Standardise a coded value: case/space-insensitive lookup, None if unknown."""
    return mapping.get(clean_text(value).lower())


# ---------- numbers ----------

def parse_amount(value):
    """'Rs.81250' / '₹1,200' / '163,970' / ' 500 ' -> Decimal. Blank or junk -> None."""
    text = clean_text(value)
    text = re.sub(r"^(rs\.?|inr|₹)\s*", "", text, flags=re.IGNORECASE).replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def parse_bool(value):
    """JSON true/false, 'true', 'True', 'FALSE' -> bool. Missing/None -> None."""
    if isinstance(value, bool):
        return value
    return BOOL_TEXT.get(clean_text(value).lower()) if value is not None else None


# ---------- dates ----------

def parse_timestamp(value):
    try:
        return datetime.strptime(clean_text(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def date_candidates(value) -> list[date]:
    """Every valid reading of a date string.

    '2026-09-30' -> one reading. '01/10/2026' -> two readings (1 Oct and
    10 Jan), because the source mixes day-first and month-first formats.
    """
    text = clean_text(value)
    if m := re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text):
        parts = [(int(m[1]), int(m[2]), int(m[3]))]
    elif m := re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        parts = [(y, b, a), (y, a, b)]          # day-first, then month-first
    else:
        return []
    found = []
    for y, mo, d in parts:
        try:
            candidate = date(y, mo, d)
        except ValueError:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def parse_date(value):
    """Return (date or None, ambiguous: bool). Day-first wins when both readings are valid."""
    candidates = date_candidates(value)
    if not candidates:
        return None, False
    return candidates[0], len(candidates) > 1


# ---------- contact details ----------

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


def clean_email(value):
    """Lowercase + trim. Returns (email or None, is_valid)."""
    email = clean_text(value).lower()
    if not email:
        return None, False
    return email, bool(EMAIL_RE.match(email))


def clean_phone(value):
    """Keep digits; a valid Indian mobile has 10 digits (optionally after 91).
    Returns ('+91-XXXXXXXXXX' or raw text, is_valid)."""
    text = clean_text(value)
    digits = re.sub(r"\D", "", text)
    if text.startswith("+91") or (len(digits) == 12 and digits.startswith("91")):
        digits = digits[2:]              # drop the country code BEFORE counting digits
    if len(digits) == 10:
        return f"+91-{digits}", True
    return (text or None), False
