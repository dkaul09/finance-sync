"""A pending transaction that disappears before settling must be removed.

The brief said pending transactions "can change amount or disappear before
settling". A dropped hold or a declined charge that stays in the sheet
overstates the budget forever. Removal is the only path that deletes
transaction data, so every guard here matters.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from finance_sync.fake_backend import FakeSheetsBackend
from finance_sync.models import Category, CategorySource, Transaction
from finance_sync.rows import TRANSACTION_COLUMNS, transaction_to_row
from finance_sync.sheets import SheetsSync
from finance_sync.upsert import FetchContext, find_vanished

NOW = dt.datetime(2026, 10, 13, 6, 0, tzinfo=dt.UTC)
WINDOW = (dt.date(2026, 10, 1), dt.date(2026, 10, 13))
ACCOUNT = "Chase Freedom Unlimited (5047)"


def txn(txn_id="T1", *, pending=True, date=dt.date(2026, 10, 11),
        amount="-52.80", account=ACCOUNT,
        source=CategorySource.RULES, category=Category.GROCERIES) -> Transaction:
    return Transaction(
        transaction_id=txn_id, date=date, account=account,
        description="TRADER JOE S #250", amount=Decimal(amount),
        category=category, pending=pending,
        posted_date=None if pending else date,
        category_source=source,
    )


def rows(*transactions: Transaction) -> list[list[str]]:
    return [transaction_to_row(t, now=NOW) for t in transactions]


def context(seen=(ACCOUNT,), errors=()) -> FetchContext:
    return FetchContext(
        window_start=WINDOW[0], window_end=WINDOW[1],
        accounts_seen=set(seen), errors=list(errors),
    )


class TestGuards:
    def test_a_vanished_pending_row_is_identified(self):
        assert find_vanished(rows(txn()), set(), context()) == ["T1"]

    def test_a_still_present_pending_row_is_kept(self):
        assert find_vanished(rows(txn()), {"T1"}, context()) == []

    def test_a_posted_row_is_never_removed(self):
        """Only pending transactions can vanish; a posted one is a fact."""
        assert find_vanished(rows(txn(pending=False)), set(), context()) == []

    def test_a_row_outside_the_fetched_window_is_kept(self):
        old = txn(date=dt.date(2026, 9, 2))
        assert find_vanished(rows(old), set(), context()) == []

    def test_nothing_is_removed_when_the_fetch_reported_errors(self):
        """A bank error means the payload is incomplete, not that charges died."""
        ctx = context(errors=["Bank needs reauthentication"])
        assert find_vanished(rows(txn()), set(), ctx) == []

    def test_nothing_is_removed_when_that_account_did_not_report(self):
        ctx = context(seen=("Some Other Account",))
        assert find_vanished(rows(txn()), set(), ctx) == []

    def test_a_manual_row_is_never_removed(self):
        """A hand-edited row is never deleted automatically."""
        manual = txn(source=CategorySource.MANUAL)
        assert find_vanished(rows(manual), set(), context()) == []

    def test_boundary_dates_are_inside_the_window(self):
        for day in (WINDOW[0], WINDOW[1]):
            assert find_vanished(rows(txn(date=day)), set(), context()) == ["T1"]

    def test_several_vanished_rows_are_all_found(self):
        existing = rows(txn("A"), txn("B"), txn("C"))
        assert sorted(find_vanished(existing, {"B"}, context())) == ["A", "C"]

    def test_blank_rows_are_ignored(self):
        assert find_vanished([[], ["", ""]], set(), context()) == []


class TestRemovalInTheSync:
    @pytest.fixture
    def backend(self) -> FakeSheetsBackend:
        backend = FakeSheetsBackend()
        sync = SheetsSync(backend)
        sid = sync.spreadsheet_for("2026-10")
        backend.append_rows(sid, "Transactions", rows(
            txn("KEEP", pending=False, amount="-40.04"),
            txn("GONE", pending=True, amount="-52.80"),
        ))
        return backend

    def _sync(self, backend, incoming, ctx=None):
        from tests.test_idempotency import keep_categories

        return SheetsSync(backend).sync(
            incoming, now=NOW, categorizer_factory=keep_categories,
            fetch=ctx or context(),
        )

    def test_the_vanished_row_is_removed(self, backend):
        self._sync(backend, [txn("KEEP", pending=False, amount="-40.04")])
        ids = [r[0] for r in backend.data_rows("Finances 2026-10")]
        assert ids == ["KEEP"]

    def test_it_is_reported_in_the_plan(self, backend):
        plans = self._sync(backend, [txn("KEEP", pending=False, amount="-40.04")])
        assert plans["2026-10"].vanished == ["GONE"]
        assert plans["2026-10"].counts["vanished"] == 1

    def test_the_surviving_row_keeps_its_values(self, backend):
        self._sync(backend, [txn("KEEP", pending=False, amount="-40.04")])
        row = backend.data_rows("Finances 2026-10")[0]
        assert row[TRANSACTION_COLUMNS.index("amount")] == "-40.04"
        assert row[TRANSACTION_COLUMNS.index("category")] == "groceries"

    def test_the_header_survives(self, backend):
        self._sync(backend, [txn("KEEP", pending=False, amount="-40.04")])
        assert backend.tab("Finances 2026-10", "Transactions")[0] == TRANSACTION_COLUMNS

    def test_the_budget_drops_by_the_vanished_amount(self, backend):
        from finance_sync.budget import BUDGET_COLUMNS

        self._sync(backend, [txn("KEEP", pending=False, amount="-40.04")])
        rows_ = backend.tab("Finances 2026-10", "Budget")[1:]
        groceries = next(r for r in rows_ if r[1] == "Groceries")
        assert groceries[BUDGET_COLUMNS.index("actual")] == "40.04"

    def test_removal_is_idempotent(self, backend):
        keep = [txn("KEEP", pending=False, amount="-40.04")]
        self._sync(backend, keep)
        plans = self._sync(backend, keep)
        assert plans["2026-10"].vanished == []
        assert len(backend.data_rows("Finances 2026-10")) == 1

    def test_nothing_is_removed_on_an_errored_fetch(self, backend):
        self._sync(backend, [txn("KEEP", pending=False, amount="-40.04")],
                   ctx=context(errors=["boom"]))
        assert len(backend.data_rows("Finances 2026-10")) == 2

    def test_a_dry_run_reports_but_does_not_remove(self, backend):
        from tests.test_idempotency import keep_categories

        plans = SheetsSync(backend, dry_run=True).sync(
            [txn("KEEP", pending=False, amount="-40.04")], now=NOW,
            categorizer_factory=keep_categories, fetch=context(),
        )
        assert plans["2026-10"].vanished == ["GONE"]
        assert len(backend.data_rows("Finances 2026-10")) == 2

    def test_no_context_means_no_removal(self, backend):
        """Without a fetch context the sync cannot prove anything vanished."""
        from tests.test_idempotency import keep_categories

        SheetsSync(backend).sync(
            [txn("KEEP", pending=False, amount="-40.04")], now=NOW,
            categorizer_factory=keep_categories,
        )
        assert len(backend.data_rows("Finances 2026-10")) == 2
