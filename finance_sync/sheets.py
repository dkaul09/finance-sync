"""Drive/Sheets layer: create-if-missing, read, and apply an upsert plan."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Protocol

from .categorize import LayeredCategorizer, apply_results, parse_overrides
from .models import Transaction
from .rows import OVERRIDE_COLUMNS, REVIEW_COLUMNS, TRANSACTION_COLUMNS
from .upsert import UpsertPlan, plan_upsert

logger = logging.getLogger(__name__)

TRANSACTIONS_TAB = "Transactions"
REVIEW_TAB = "Review"
OVERRIDES_TAB = "Overrides"

TAB_HEADERS = {
    TRANSACTIONS_TAB: TRANSACTION_COLUMNS,
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
            return spreadsheet_id

        folder_id = self._folder()
        title = spreadsheet_title(month_key)

        logger.info("Creating spreadsheet %s", title)
        spreadsheet_id = self._backend.create_spreadsheet(folder_id, title, TAB_HEADERS)
        self._carry_overrides_forward(folder_id, spreadsheet_id, month_key)
        return spreadsheet_id

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
            categorized = apply_results(month_txns, categorizer.categorize(month_txns))

            existing = self._backend.read_tab(spreadsheet_id, TRANSACTIONS_TAB)
            body = existing[1:] if existing else []
            plans[month_key] = plan_upsert(body, categorized, now=now)
        return plans

    def apply(self, plans: dict[str, UpsertPlan]) -> None:
        """Write each plan back. Two write calls per spreadsheet, at most."""
        if self._dry_run:
            logger.info("Dry run: no writes")
            return

        for month_key, month_plan in plans.items():
            spreadsheet_id = self.spreadsheet_for(month_key)
            if month_plan.updates:
                self._backend.update_rows(spreadsheet_id, TRANSACTIONS_TAB, month_plan.updates)
            if month_plan.inserts:
                self._backend.append_rows(spreadsheet_id, TRANSACTIONS_TAB, month_plan.inserts)

    def sync(
        self,
        transactions: list[Transaction],
        now: dt.datetime | None = None,
        categorizer_factory=None,
    ) -> dict[str, UpsertPlan]:
        plans = self.plan(transactions, now=now, categorizer_factory=categorizer_factory)
        self.apply(plans)
        return plans

    def read_overrides(self, month_key: str) -> list[list[str]]:
        spreadsheet_id = self.spreadsheet_for(month_key)
        rows = self._backend.read_tab(spreadsheet_id, OVERRIDES_TAB)
        return rows[1:] if rows else []

    def append_overrides(self, month_key: str, rows: list[list[str]]) -> None:
        if not rows or self._dry_run:
            return
        self._backend.append_rows(self.spreadsheet_for(month_key), OVERRIDES_TAB, rows)
