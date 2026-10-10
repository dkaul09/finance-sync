"""The Budget tab: planned vs actual spending per category, per month."""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal, InvalidOperation

from .models import BUDGET_LINES, DISPLAY_NAMES, NON_BUDGET, Category, Transaction

logger = logging.getLogger(__name__)

BUDGET_COLUMNS = ["category", "line", "planned", "actual", "difference", "last_updated"]

#: Row 1 is the header, row 2 is Totals, budget lines start at row 3.
TOTALS_ROW = 2
FIRST_LINE_ROW = 3


def parse_money(raw: str) -> Decimal:
    """Parse a planned amount. Tolerates `$1,355`, blanks and junk."""
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return Decimal("0")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = Decimal(text)
    except InvalidOperation:
        return Decimal("0")
    return -value if negative else value


def actuals_by_category(transactions: list[Transaction]) -> dict[Category, Decimal]:
    """Sum spending per category.

    Spending only: positive amounts (money in) are ignored, so a refund or a
    deposit cannot inflate a spending line. Pending transactions are included,
    so the number reflects today rather than lagging settlement.
    """
    totals: dict[Category, Decimal] = {}
    for txn in transactions:
        if txn.category in NON_BUDGET:
            continue
        if txn.amount >= 0:
            continue
        totals[txn.category] = totals.get(txn.category, Decimal("0")) + (-txn.amount)
    return totals


def default_rows(now: dt.datetime | None = None) -> list[list[str]]:
    """The Budget tab as first created, pre-filled with planned amounts."""
    stamp = (now or dt.datetime.now(dt.UTC)).replace(microsecond=0).isoformat()
    rows = [["", "Totals", "0", "0", "0", stamp]]
    rows += [
        [category.value, name, planned, "0", "0", stamp]
        for category, name, planned in BUDGET_LINES
    ]
    return rows


def read_planned(rows: list[list[str]]) -> dict[Category, Decimal]:
    """Read hand-edited planned amounts from existing Budget rows."""
    planned: dict[Category, Decimal] = {}
    for row in rows:
        padded = list(row) + [""] * len(BUDGET_COLUMNS)
        key = (padded[0] or "").strip().lower()
        if not key or key == "category":
            continue
        try:
            category = Category(key)
        except ValueError:
            logger.warning("Budget row has unknown category %r; ignoring", key)
            continue
        planned[category] = parse_money(padded[2])
    return planned


def build_rows(
    transactions: list[Transaction],
    existing: list[list[str]] | None = None,
    now: dt.datetime | None = None,
) -> list[list[str]]:
    """Build the full Budget tab body: Totals first, then one row per line.

    Planned amounts come from the sheet when present, so hand-edited budgets
    are preserved; actuals are always recomputed from this month's
    transactions.
    """
    stamp = (now or dt.datetime.now(dt.UTC)).replace(microsecond=0).isoformat()
    planned_overrides = read_planned(existing or [])
    actuals = actuals_by_category(transactions)

    body: list[list[str]] = []
    planned_total = Decimal("0")
    actual_total = Decimal("0")

    for category, name, default_planned in BUDGET_LINES:
        planned = planned_overrides.get(category, Decimal(default_planned))
        actual = actuals.get(category, Decimal("0"))
        planned_total += planned
        actual_total += actual
        body.append(
            [
                category.value,
                name,
                f"{planned:.2f}",
                f"{actual:.2f}",
                f"{actual - planned:.2f}",
                stamp,
            ]
        )

    totals = [
        "",
        "Totals",
        f"{planned_total:.2f}",
        f"{actual_total:.2f}",
        f"{actual_total - planned_total:.2f}",
        stamp,
    ]
    return [totals] + body


def uncategorized_spend(transactions: list[Transaction]) -> Decimal:
    """Spending that landed in MISCL, i.e. the catch-all's size."""
    return actuals_by_category(transactions).get(Category.MISCL, Decimal("0"))
