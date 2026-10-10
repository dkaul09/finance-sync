"""Drive/Sheets layer: create-if-missing, read, and apply an upsert plan."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Protocol

from .budget import BUDGET_COLUMNS, FIRST_LINE_ROW, build_rows, default_rows
from .categorize import LayeredCategorizer, apply_results, parse_overrides
from .models import Transaction
from .rows import OVERRIDE_COLUMNS, REVIEW_COLUMNS, TRANSACTION_COLUMNS
from .rows import review_row_ids, row_to_transaction, transaction_to_review_row
from .upsert import UpsertPlan, plan_upsert

logger = logging.getLogger(__name__)

TRANSACTIONS_TAB = "Transactions"
REVIEW_TAB = "Review"
OVERRIDES_TAB = "Overrides"
BUDGET_TAB = "Budget"

TAB_HEADERS = {
    TRANSACTIONS_TAB: TRANSACTION_COLUMNS,
    BUDGET_TAB: BUDGET_COLUMNS,
    REVIEW_TAB: REVIEW_COLUMNS,
    OVERRIDES_TAB: OVERRIDE_COLUMNS,
}


def spreadsheet_title(month_key: str) -> str:
    """`2026-10` -> `Finances 2026-10`."""
    return f"Finances {month_key}"


class SheetsBackend(Protocol):
    """The narrow surface the sync needs. Implemented by Google and by a fake."""

    def ensure_folder(self, name: str) -> str: ...

    def find_folder(self, name: str) -> str | None: ...

    def find_spreadsheet(self, folder_id: str, title: str) -> str | None: ...

    def create_spreadsheet(self, folder_id: str, title: str, tabs: dict[str, list[str]]) -> str: ...

    def list_spreadsheets(self, folder_id: str) -> list[tuple[str, str]]: ...

    def list_tabs(self, spreadsheet_id: str) -> list[str]: ...

    def add_tab(self, spreadsheet_id: str, name: str, header: list[str]) -> None: ...

    def clear_tab(self, spreadsheet_id: str, tab: str, keep_header: bool = True) -> None: ...

    def read_tab(self, spreadsheet_id: str, tab: str) -> list[list[str]]: ...

    def update_rows(self, spreadsheet_id: str, tab: str, updates: list[tuple[int, list[str]]]) -> None: ...

    def append_rows(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None: ...


class SheetsSync:
    """Routes transactions to monthly spreadsheets and upserts them."""

    def __init__(
        self,
        backend: SheetsBackend,
        folder_name: str = "Finances",
        dry_run: bool = False,
    ) -> None:
        self._backend = backend
        self._folder_name = folder_name
        self._dry_run = dry_run
        self._folder_id: str | None = None

    def _folder(self) -> str:
        if self._folder_id is None:
            self._folder_id = self._backend.ensure_folder(self._folder_name)
        return self._folder_id

    def _folder_readonly(self) -> str | None:
        """Resolve the folder without creating it. Used by dry runs."""
        if self._folder_id is None:
            self._folder_id = self._backend.find_folder(self._folder_name)
        return self._folder_id

    def find_spreadsheet(self, month_key: str) -> str | None:
        """Look up `Finances YYYY-MM` without creating anything.

        Resolves the folder read-only during a dry run, so planning cannot
        create the folder as a side effect of looking inside it.
        """
        folder_id = self._folder_readonly() if self._dry_run else self._folder()
        if folder_id is None:
            return None
        return self._backend.find_spreadsheet(folder_id, spreadsheet_title(month_key))

    def spreadsheet_for(self, month_key: str) -> str:
        """Find or create `Finances YYYY-MM`. This is the month-rollover path.

        Called with the transaction's own month, so no run ever needs to know
        what "the current month" is.
        """
        spreadsheet_id = self.find_spreadsheet(month_key)
        if spreadsheet_id is not None:
            if not self._dry_run:
                self._ensure_tabs(spreadsheet_id)
            return spreadsheet_id

        folder_id = self._folder()
        title = spreadsheet_title(month_key)

        logger.info("Creating spreadsheet %s", title)
        spreadsheet_id = self._backend.create_spreadsheet(folder_id, title, TAB_HEADERS)
        self._carry_overrides_forward(folder_id, spreadsheet_id, month_key)
        self._seed_budget(folder_id, spreadsheet_id, month_key)
        return spreadsheet_id

    def _ensure_tabs(self, spreadsheet_id: str) -> None:
        """Add any tab this version expects but an older spreadsheet lacks.

        Spreadsheets created by an earlier version predate the Budget tab, and
        reading a missing tab is a hard 400 from the Sheets API. Migrating in
        place is cheaper than asking the user to delete and re-sync.
        """
        existing = set(self._backend.list_tabs(spreadsheet_id))
        for name, header in TAB_HEADERS.items():
            if name in existing:
                continue
            logger.info("Adding missing %r tab to an existing spreadsheet", name)
            self._backend.add_tab(spreadsheet_id, name, header)
            if name == BUDGET_TAB:
                self._backend.append_rows(spreadsheet_id, BUDGET_TAB, default_rows())

    def _seed_budget(self, folder_id: str, spreadsheet_id: str, month_key: str) -> None:
        """Populate a new Budget tab, inheriting planned amounts if possible.

        Planned amounts are hand-maintained, so like Overrides they carry
        forward from the newest earlier month; actuals are not copied, since
        they belong to that month.
        """
        previous = self._latest_previous_spreadsheet(folder_id, month_key)
        rows = None
        if previous is not None:
            earlier = self._backend.read_tab(previous, BUDGET_TAB)
            if earlier and len(earlier) > 1:
                rows = build_rows([], existing=earlier[1:])
                logger.info("Carrying planned budget forward into %s", month_key)
        if rows is None:
            rows = default_rows()
        self._backend.append_rows(spreadsheet_id, BUDGET_TAB, rows)

    def _carry_overrides_forward(
        self, folder_id: str, spreadsheet_id: str, month_key: str
    ) -> None:
        """Copy the newest earlier month's Overrides into a fresh spreadsheet.

        Overrides are hand-maintained, so a per-month tab would silently reset
        every rollover and the "never ask about this merchant again" promise
        would last at most 31 days.
        """
        previous = self._latest_previous_spreadsheet(folder_id, month_key)
        if previous is None:
            return
        rows = self._backend.read_tab(previous, OVERRIDES_TAB)
        body = rows[1:] if rows else []
        body = [r for r in body if any((c or "").strip() for c in r)]
        if not body:
            return
        logger.info("Carrying %s override(s) forward into %s", len(body), month_key)
        self._backend.append_rows(spreadsheet_id, OVERRIDES_TAB, body)

    def _latest_previous_spreadsheet(self, folder_id: str, month_key: str) -> str | None:
        """The most recent `Finances YYYY-MM` strictly before `month_key`."""
        candidates = []
        for title, sid in self._backend.list_spreadsheets(folder_id):
            if not title.startswith("Finances "):
                continue
            other = title.removeprefix("Finances ").strip()
            if len(other) == 7 and other < month_key:
                candidates.append((other, sid))
        if not candidates:
            return None
        return max(candidates)[1]

    def plan(
        self,
        transactions: list[Transaction],
        now: dt.datetime | None = None,
        categorizer_factory=None,
    ) -> dict[str, UpsertPlan]:
        """Categorize then diff, one plan per target month. Reads, never writes.

        Overrides are read per spreadsheet, so each month's rules come from
        that month's own tab (seeded from the previous month on creation).
        """
        by_month: dict[str, list[Transaction]] = {}
        for txn in transactions:
            by_month.setdefault(txn.month_key, []).append(txn)

        plans: dict[str, UpsertPlan] = {}
        for month_key in sorted(by_month):
            # A dry run must not create anything, so resolve without creating
            # and plan against an empty sheet when it does not exist yet.
            if self._dry_run:
                spreadsheet_id = self.find_spreadsheet(month_key)
                if spreadsheet_id is None:
                    month_txns = by_month[month_key]
                    categorized = apply_results(
                        month_txns,
                        (categorizer_factory(  # type: ignore[misc]
                            []
                        ) if categorizer_factory is not None
                         else LayeredCategorizer()).categorize(month_txns),
                    )
                    plan = plan_upsert([], categorized, now=now)
                    plan.would_create = True
                    plans[month_key] = plan
                    continue
            else:
                spreadsheet_id = self.spreadsheet_for(month_key)

            override_rows = self._backend.read_tab(spreadsheet_id, OVERRIDES_TAB)
            overrides = parse_overrides(override_rows[1:] if override_rows else [])

            if categorizer_factory is not None:
                categorizer = categorizer_factory(overrides)
            else:
                categorizer = LayeredCategorizer(overrides=overrides)

            month_txns = by_month[month_key]
            results = categorizer.categorize(month_txns)
            categorized = apply_results(month_txns, results)

            # Low-confidence proposals go to Review rather than Transactions,
            # so an uncertain guess never silently becomes a fact.
            low_confidence = {r.transaction_id for r in results if r.needs_review}
            existing_transactions = self._backend.read_tab(
                spreadsheet_id, TRANSACTIONS_TAB
            )
            body = existing_transactions[1:] if existing_transactions else []
            existing_by_id = {}
            for row in body:
                parsed = row_to_transaction(row)
                if parsed is not None:
                    existing_by_id[parsed.transaction_id] = parsed

            for_transactions = []
            for txn in categorized:
                if txn.transaction_id not in low_confidence:
                    for_transactions.append(txn)
                    continue
                existing = existing_by_id.get(txn.transaction_id)
                if existing is not None:
                    # Already on the sheet: keep maintaining its financial
                    # fields, but keep whatever category is already there
                    # rather than stamping an unconfident guess over it.
                    for_transactions.append(
                        txn.with_category(
                            existing.category,
                            existing.category_source,
                            confidence=existing.confidence,
                            recurring=existing.recurring,
                            business_expense=existing.business_expense,
                        )
                    )
                # Otherwise it is left out of Transactions entirely until a
                # human resolves it on the Review tab.

            for_review = [t for t in categorized if t.transaction_id in low_confidence]

            plan = plan_upsert(body, for_transactions, now=now)
            plan.review = for_review
            plans[month_key] = plan
        return plans

    def apply(self, plans: dict[str, UpsertPlan]) -> None:
        """Write each plan back, then refresh that month's Budget actuals."""
        if self._dry_run:
            logger.info("Dry run: no writes")
            return

        for month_key, month_plan in plans.items():
            spreadsheet_id = self.spreadsheet_for(month_key)
            if month_plan.updates:
                self._backend.update_rows(spreadsheet_id, TRANSACTIONS_TAB, month_plan.updates)
            if month_plan.inserts:
                self._backend.append_rows(spreadsheet_id, TRANSACTIONS_TAB, month_plan.inserts)
            self._write_review(spreadsheet_id, month_plan)
            self.refresh_budget(spreadsheet_id)

    def _write_review(self, spreadsheet_id: str, plan: UpsertPlan) -> None:
        """Append new low-confidence proposals, skipping ones already listed."""
        if not plan.review:
            return
        existing = self._backend.read_tab(spreadsheet_id, REVIEW_TAB)
        listed = review_row_ids(existing[1:] if existing else [])
        rows = [
            transaction_to_review_row(txn)
            for txn in plan.review
            if txn.transaction_id not in listed
        ]
        if rows:
            logger.info("Routing %s transaction(s) to Review", len(rows))
            self._backend.append_rows(spreadsheet_id, REVIEW_TAB, rows)

    def refresh_budget(self, spreadsheet_id: str) -> list[list[str]]:
        """Recompute Planned vs Actual from the whole Transactions tab.

        Actuals are derived from what is in the sheet, not from this run's
        batch, so a partial sync can never produce a partial month total.
        Planned amounts already in the tab are preserved.
        """
        rows = self._backend.read_tab(spreadsheet_id, TRANSACTIONS_TAB)
        transactions = [
            parsed
            for row in (rows[1:] if rows else [])
            if (parsed := row_to_transaction(row)) is not None
        ]

        existing = self._backend.read_tab(spreadsheet_id, BUDGET_TAB)
        existing_body = existing[1:] if existing else []
        body = build_rows(transactions, existing=existing_body)

        # Only write when something actually moved. last_updated is excluded
        # from the comparison for the same reason it is on Transactions:
        # restamping it every run would make every sync a write.
        def significant(rows: list[list[str]]) -> list[list[str]]:
            return [row[:5] for row in rows]

        if significant(existing_body) == significant(body):
            return body

        updates = [(FIRST_LINE_ROW - 1 + i, row) for i, row in enumerate(body)]
        self._backend.update_rows(spreadsheet_id, BUDGET_TAB, updates)
        return body

    def sync(
        self,
        transactions: list[Transaction],
        now: dt.datetime | None = None,
        categorizer_factory=None,
    ) -> dict[str, UpsertPlan]:
        plans = self.plan(transactions, now=now, categorizer_factory=categorizer_factory)
        self.apply(plans)
        return plans

    def month_keys(self) -> list[str]:
        """Every `Finances YYYY-MM` spreadsheet in the folder, oldest first."""
        folder_id = self._folder_readonly() if self._dry_run else self._folder()
        if folder_id is None:
            return []
        months = []
        for title, _ in self._backend.list_spreadsheets(folder_id):
            if not title.startswith("Finances "):
                continue
            key = title.removeprefix("Finances ").strip()
            if len(key) == 7 and key[4] == "-":
                months.append(key)
        return sorted(months)

    def apply_review(self, month_key: str, outcome) -> None:
        """Write a review pass back: promote, record overrides, trim Review."""
        if self._dry_run:
            logger.info("Dry run: review changes not written")
            return

        spreadsheet_id = self.spreadsheet_for(month_key)

        if outcome.promoted:
            existing = self._backend.read_tab(spreadsheet_id, TRANSACTIONS_TAB)
            body = existing[1:] if existing else []
            plan = plan_upsert(body, outcome.promoted)
            if plan.updates:
                self._backend.update_rows(spreadsheet_id, TRANSACTIONS_TAB, plan.updates)
            if plan.inserts:
                self._backend.append_rows(spreadsheet_id, TRANSACTIONS_TAB, plan.inserts)

        if outcome.new_overrides:
            self._backend.append_rows(
                spreadsheet_id, OVERRIDES_TAB, outcome.new_overrides
            )

        # Rewrite Review with only what is still pending.
        self._backend.clear_tab(spreadsheet_id, REVIEW_TAB)
        if outcome.remaining:
            self._backend.append_rows(spreadsheet_id, REVIEW_TAB, outcome.remaining)

        self.refresh_budget(spreadsheet_id)

    def review_rows(self, month_key: str) -> list[list[str]]:
        spreadsheet_id = self.spreadsheet_for(month_key)
        rows = self._backend.read_tab(spreadsheet_id, REVIEW_TAB)
        return rows[1:] if rows else []

    def override_rows(self, month_key: str) -> list[list[str]]:
        spreadsheet_id = self.spreadsheet_for(month_key)
        rows = self._backend.read_tab(spreadsheet_id, OVERRIDES_TAB)
        return rows[1:] if rows else []

    def budget_rows(self, spreadsheet_id: str) -> list[list[str]]:
        """The Budget tab body, for display."""
        rows = self._backend.read_tab(spreadsheet_id, BUDGET_TAB)
        return rows[1:] if rows else []

    def read_overrides(self, month_key: str) -> list[list[str]]:
        spreadsheet_id = self.spreadsheet_for(month_key)
        rows = self._backend.read_tab(spreadsheet_id, OVERRIDES_TAB)
        return rows[1:] if rows else []

    def append_overrides(self, month_key: str, rows: list[list[str]]) -> None:
        if not rows or self._dry_run:
            return
        self._backend.append_rows(self.spreadsheet_for(month_key), OVERRIDES_TAB, rows)
