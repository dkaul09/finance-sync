"""The upsert core: decide what to insert, update, and leave alone.

This module is pure — it takes existing rows and incoming transactions and
returns a plan. No I/O, which is what makes the idempotency guarantees
testable.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, replace

from .models import CategorySource, Transaction
from .rows import comparable, row_to_transaction, transaction_to_row

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchContext:
    """What one fetch actually covered.

    Removal is the only operation that deletes transaction data, so it needs
    proof that an absent transaction is genuinely gone rather than merely
    outside the window or missing from a partial response.
    """

    window_start: dt.date
    window_end: dt.date
    #: Account labels that appeared in the response.
    accounts_seen: set[str] = field(default_factory=set)
    #: SimpleFIN's errors array. Non-empty means the payload is untrustworthy.
    errors: list[str] = field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        return not self.errors


def find_vanished(
    existing_rows: list[list[str]],
    incoming_ids: set[str],
    context: FetchContext,
) -> list[str]:
    """Transaction ids for pending rows the bank no longer reports.

    Every guard below exists to stop a transient API condition from deleting
    real data:

    * only `pending` rows can vanish -- a posted transaction is a fact
    * the row's date must fall inside the window actually fetched
    * the response must have carried no errors
    * the row's account must have reported in this response
    * `manual` rows are never removed automatically, only left alone
    """
    if not context.trustworthy:
        return []

    vanished: list[str] = []
    for row in existing_rows:
        parsed = row_to_transaction(row)
        if parsed is None or not parsed.pending:
            continue
        if parsed.transaction_id in incoming_ids:
            continue
        if not (context.window_start <= parsed.date <= context.window_end):
            continue
        if parsed.account not in context.accounts_seen:
            continue
        if parsed.category_source is CategorySource.MANUAL:
            logger.warning(
                "Pending transaction %s (%s) is no longer reported, but it was "
                "categorized by hand; leaving it in place",
                parsed.transaction_id, parsed.description,
            )
            continue
        vanished.append(parsed.transaction_id)
    return vanished


@dataclass
class UpsertPlan:
    """What a sync would do to one spreadsheet."""

    #: (1-based sheet row number, serialized row) for rows that change.
    updates: list[tuple[int, list[str]]] = field(default_factory=list)
    #: serialized rows to append.
    inserts: list[list[str]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    #: transaction ids whose hand-edited category was protected.
    preserved_manual: list[str] = field(default_factory=list)
    #: set when a dry run would have had to create the spreadsheet.
    would_create: bool = False
    #: transactions routed to the Review tab instead of Transactions.
    review: list = field(default_factory=list)
    #: ids of pending transactions the bank stopped reporting.
    vanished: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "inserted": len(self.inserts),
            "updated": len(self.updates),
            "unchanged": len(self.unchanged),
            "routed_to_review": len(self.review),
            "vanished": len(self.vanished),
        }

    def describe(self) -> str:
        lines = []
        if self.would_create:
            lines.append("  spreadsheet: would be CREATED")
        lines += [
            f"  inserts:   {len(self.inserts)}",
            f"  updates:   {len(self.updates)}",
            f"  unchanged: {len(self.unchanged)}",
        ]
        if self.review:
            lines.append(f"  to review: {len(self.review)}")
        if self.vanished:
            lines.append(f"  vanished:  {len(self.vanished)} (pending, no longer reported)")
        if self.preserved_manual:
            lines.append(
                f"  manual categories preserved: {', '.join(self.preserved_manual)}"
            )
        return "\n".join(lines)


def _merge_with_existing(
    incoming: Transaction, existing: Transaction
) -> tuple[Transaction, bool]:
    """Combine an incoming transaction with the row already in the sheet.

    Returns the transaction to write and whether a manual category was
    protected. A `manual` row keeps its category, source and derived flags;
    only the financial fields move. This is the "never overwrite a category I
    edited by hand" rule, and it holds through pending->posted transitions and
    amount changes alike.
    """
    if existing.category_source is CategorySource.MANUAL:
        return (
            replace(
                incoming,
                category=existing.category,
                category_source=CategorySource.MANUAL,
                confidence=existing.confidence,
                recurring=existing.recurring,
                business_expense=existing.business_expense,
            ),
            True,
        )
    return incoming, False


def plan_upsert(
    existing_rows: list[list[str]],
    incoming: list[Transaction],
    now: dt.datetime | None = None,
    header_offset: int = 1,
) -> UpsertPlan:
    """Diff incoming transactions against the sheet's current contents.

    Keyed strictly on `transaction_id`, so running twice in a day is a no-op
    and a settling transaction updates its row in place instead of appending a
    duplicate.
    """
    now = now or dt.datetime.now(dt.UTC)
    plan = UpsertPlan()

    # transaction_id -> (1-based row number, parsed transaction)
    index: dict[str, tuple[int, Transaction]] = {}
    for offset, row in enumerate(existing_rows):
        parsed = row_to_transaction(row)
        if parsed is None:
            continue
        index[parsed.transaction_id] = (offset + 1 + header_offset, parsed)

    seen: set[str] = set()
    for txn in incoming:
        # Guard against a payload containing the same id twice.
        if txn.transaction_id in seen:
            continue
        seen.add(txn.transaction_id)

        match = index.get(txn.transaction_id)
        if match is None:
            plan.inserts.append(transaction_to_row(txn, now=now))
            continue

        row_number, existing = match
        merged, preserved = _merge_with_existing(txn, existing)
        if preserved:
            plan.preserved_manual.append(txn.transaction_id)

        if comparable(merged) == comparable(existing):
            plan.unchanged.append(txn.transaction_id)
            continue

        plan.updates.append((row_number, transaction_to_row(merged, now=now)))

    return plan
