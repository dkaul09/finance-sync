"""Row <-> Transaction serialization for the Transactions tab.

Kept separate from the Sheets transport so the upsert logic can be tested
without any Google dependency.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from .models import Category, CategorySource, Transaction

#: Column order of the Transactions tab. Appending is safe; reordering is not.
TRANSACTION_COLUMNS = [
    "transaction_id",
    "date",
    "posted_date",
    "account",
    "description",
    "amount",
    "category",
    "pending",
    "category_source",
    "confidence",
    "last_updated",
    "recurring",
    "business_expense",
]

REVIEW_COLUMNS = [
    "transaction_id",
    "date",
    "account",
    "description",
    "amount",
    "proposed_category",
    "confidence",
    "category",
    "recurring",
    "business_expense",
    "last_updated",
]

OVERRIDE_COLUMNS = ["pattern", "category", "is_regex"]

#: Fields compared to decide insert / update / unchanged.
#: `last_updated` is deliberately excluded: stamping it every run would make
#: every row look changed and defeat idempotency entirely.
COMPARED_FIELDS = [
    "date",
    "posted_date",
    "account",
    "description",
    "amount",
    "category",
    "pending",
    "category_source",
    "confidence",
    "recurring",
    "business_expense",
]


def _fmt_date(value: dt.date | None) -> str:
    return value.isoformat() if value else ""


def _fmt_float(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def transaction_to_row(txn: Transaction, now: dt.datetime | None = None) -> list[str]:
    """Serialize a transaction into a Transactions-tab row."""
    stamp = txn.last_updated or now or dt.datetime.now(dt.UTC)
    return [
        txn.transaction_id,
        _fmt_date(txn.date),
        _fmt_date(txn.posted_date),
        txn.account,
        txn.description,
        str(txn.amount),
        str(txn.category),
        "TRUE" if txn.pending else "FALSE",
        str(txn.category_source),
        _fmt_float(txn.confidence),
        stamp.replace(microsecond=0).isoformat(),
        _fmt_float(txn.recurring),
        _fmt_float(txn.business_expense),
    ]


def _parse_date(raw: str) -> dt.date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_decimal(raw: str) -> Decimal:
    raw = (raw or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal("0")


def _parse_bool(raw: str) -> bool:
    return (raw or "").strip().upper() in {"TRUE", "YES", "1"}


def _enum(value: str, enum_cls, default):
    try:
        return enum_cls((value or "").strip().lower())
    except ValueError:
        return default


def row_to_transaction(row: list[str]) -> Transaction | None:
    """Parse a Transactions-tab row. Returns None for blank or headerless rows.

    Tolerant by design: a sheet is hand-editable, so a half-filled or
    short row must not crash a scheduled run.
    """
    padded = list(row) + [""] * (len(TRANSACTION_COLUMNS) - len(row))
    cells = dict(zip(TRANSACTION_COLUMNS, padded))

    txn_id = (cells["transaction_id"] or "").strip()
    if not txn_id or txn_id == "transaction_id":
        return None

    date = _parse_date(cells["date"])
    if date is None:
        return None

    stamp = (cells["last_updated"] or "").strip()
    try:
        last_updated = dt.datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        last_updated = None

    return Transaction(
        transaction_id=txn_id,
        date=date,
        posted_date=_parse_date(cells["posted_date"]),
        account=(cells["account"] or "").strip(),
        description=(cells["description"] or "").strip(),
        amount=_parse_decimal(cells["amount"]),
        category=_enum(cells["category"], Category, Category.OTHER),
        pending=_parse_bool(cells["pending"]),
        category_source=_enum(cells["category_source"], CategorySource, CategorySource.NONE),
        confidence=_parse_float(cells["confidence"]),
        recurring=_parse_float(cells["recurring"]),
        business_expense=_parse_float(cells["business_expense"]),
        last_updated=last_updated,
    )


def comparable(txn: Transaction) -> tuple:
    """The tuple used to decide whether a row actually changed."""
    values = []
    for field in COMPARED_FIELDS:
        value = getattr(txn, field)
        if isinstance(value, Decimal):
            # Normalize so "-18.75" and "-18.750" compare equal.
            value = value.normalize()
        if isinstance(value, float):
            value = round(value, 4)
        values.append(str(value) if value is not None else "")
    return tuple(values)
