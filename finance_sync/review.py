"""The feedback loop: fold reviewed rows back into Transactions.

Scans the Review tab for rows where a category has been filled in by hand,
moves them into Transactions as `manual`, and appends a matching Overrides
rule so that merchant is never asked about again.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

from .categorize import normalize
from .models import Category, CategorySource, Transaction
from .rows import REVIEW_COLUMNS

logger = logging.getLogger(__name__)

#: Tokens that are never part of a merchant name: store numbers, state codes,
#: dates, and the noise banks append to descriptions.
_STATE_CODES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","USA",
}
_NOISE = {"INC", "LLC", "CO", "CORP", "COM", "WWW", "THE", "PMTS", "WEB", "ID"}

#: How many tokens a generated override pattern may span.
MAX_PATTERN_TOKENS = 2


def suggest_pattern(description: str, payee: str = "") -> str:
    """Derive a reusable Overrides pattern from a transaction description.

    `VERCEL INC. VERCEL.COM CA 09/18` -> `VERCEL`
    `INSTRUCTURE PARCHMEN INSTRUCTURE.C UT 09/27` -> `INSTRUCTURE PARCHMEN`

    Deliberately conservative: a pattern that is too broad would silently
    recategorize unrelated merchants, which is worse than asking again.
    """
    source = payee.strip() or description
    tokens = normalize(source).split()

    kept: list[str] = []
    for token in tokens:
        if token.isdigit():
            break
        if token in _STATE_CODES or token in _NOISE:
            break
        # Store codes like T3217 or 25043: letters followed by digits.
        if re.fullmatch(r"[A-Z]{0,2}\d{3,}", token):
            break
        kept.append(token)
        if len(kept) >= MAX_PATTERN_TOKENS:
            break

    return " ".join(kept) if kept else normalize(source)[:24].strip()


@dataclass
class ReviewOutcome:
    """What a review pass did to one spreadsheet."""

    month_key: str
    promoted: list[Transaction] = field(default_factory=list)
    new_overrides: list[list[str]] = field(default_factory=list)
    remaining: list[list[str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "promoted": len(self.promoted),
            "overrides_added": len(self.new_overrides),
            "still_pending": len(self.remaining),
            "skipped": len(self.skipped),
        }


def _cell(row: list[str], name: str) -> str:
    index = REVIEW_COLUMNS.index(name)
    return (row[index] if index < len(row) else "") or ""


def _to_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def process_rows(
    review_rows: list[list[str]],
    existing_overrides: list[list[str]],
    now: dt.datetime | None = None,
) -> ReviewOutcome:
    """Split Review rows into promoted, pending, and unusable.

    A row counts as reviewed only when its `category` cell holds a valid
    category. Anything else stays on the tab rather than being guessed at.
    """
    outcome = ReviewOutcome(month_key="")
    seen_patterns = {
        normalize((row[0] if row else "") or "") for row in existing_overrides
    }
    seen_patterns.discard("")

    for row in review_rows:
        if not any((cell or "").strip() for cell in row):
            continue

        txn_id = _cell(row, "transaction_id").strip()
        if not txn_id or txn_id == "transaction_id":
            continue

        raw_category = _cell(row, "category").strip().lower()
        if not raw_category:
            outcome.remaining.append(row)
            continue

        try:
            category = Category(raw_category)
        except ValueError:
            logger.error(
                "Review row %s has unknown category %r; leaving it on the tab",
                txn_id, raw_category,
            )
            outcome.skipped.append(txn_id)
            outcome.remaining.append(row)
            continue

        date_text = _cell(row, "date").strip()
        try:
            date = dt.date.fromisoformat(date_text)
        except ValueError:
            logger.error("Review row %s has unusable date %r; skipping", txn_id, date_text)
            outcome.skipped.append(txn_id)
            outcome.remaining.append(row)
            continue

        amount_text = _cell(row, "amount").strip().replace("$", "").replace(",", "")
        try:
            amount = Decimal(amount_text or "0")
        except Exception:  # noqa: BLE001
            logger.error("Review row %s has unusable amount %r; skipping", txn_id, amount_text)
            outcome.skipped.append(txn_id)
            outcome.remaining.append(row)
            continue

        description = _cell(row, "description")
        outcome.promoted.append(
            Transaction(
                transaction_id=txn_id,
                date=date,
                account=_cell(row, "account"),
                description=description,
                amount=amount,
                category=category,
                category_source=CategorySource.MANUAL,
                recurring=_to_float(_cell(row, "recurring")),
                business_expense=_to_float(_cell(row, "business_expense")),
            )
        )

        pattern = suggest_pattern(description)
        if pattern and normalize(pattern) not in seen_patterns:
            seen_patterns.add(normalize(pattern))
            outcome.new_overrides.append([pattern, category.value, "FALSE"])
        else:
            logger.info("Override for %r already exists; not duplicating", pattern)

    return outcome
