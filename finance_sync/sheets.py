"""Drive/Sheets layer: create-if-missing, read, and apply an upsert plan."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Protocol

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

    def find_spreadsheet(self, folder_id: str, title: str) -> str | None: ...

    def create_spreadsheet(self, folder_id: str, title: str, tabs: dict[str, list[str]]) -> str: ...

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

    def spreadsheet_for(self, month_key: str) -> str:
        """Find or create `Finances YYYY-MM`. This is the month-rollover path.

        Called with the transaction's own month, so no run ever needs to know
        what "the current month" is.
        """
        folder_id = self._folder()
        title = spreadsheet_title(month_key)

        existing = self._backend.find_spreadsheet(folder_id, title)
        if existing is not None:
            return existing

        logger.info("Creating spreadsheet %s", title)
        return self._backend.create_spreadsheet(folder_id, title, TAB_HEADERS)

    def plan(self, transactions: list[Transaction], now: dt.datetime | None = None) -> dict[str, UpsertPlan]:
        """Build one plan per target month. Reads, never writes."""
        by_month: dict[str, list[Transaction]] = {}
        for txn in transactions:
            by_month.setdefault(txn.month_key, []).append(txn)

        plans: dict[str, UpsertPlan] = {}
        for month_key in sorted(by_month):
            spreadsheet_id = self.spreadsheet_for(month_key)
            existing = self._backend.read_tab(spreadsheet_id, TRANSACTIONS_TAB)
            body = existing[1:] if existing else []
            plans[month_key] = plan_upsert(body, by_month[month_key], now=now)
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

    def sync(self, transactions: list[Transaction], now: dt.datetime | None = None) -> dict[str, UpsertPlan]:
        plans = self.plan(transactions, now=now)
        self.apply(plans)
        return plans

    def read_overrides(self, month_key: str) -> list[list[str]]:
        spreadsheet_id = self.spreadsheet_for(month_key)
        rows = self._backend.read_tab(spreadsheet_id, OVERRIDES_TAB)
        return rows[1:] if rows else []
