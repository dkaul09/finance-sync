"""Budget tab: planned vs actual, and the rules about what counts."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from finance_sync.budget import (
    BUDGET_COLUMNS,
    actuals_by_category,
    build_rows,
    default_rows,
    parse_money,
    read_planned,
)
from finance_sync.fake_backend import FakeSheetsBackend
from finance_sync.models import BUDGET_LINES, NON_BUDGET, Category, Transaction
from finance_sync.sheets import SheetsSync

NOW = dt.datetime(2026, 10, 9, 6, 0, tzinfo=dt.UTC)


def txn(category: Category, amount: str, txn_id: str = "T1", pending: bool = False) -> Transaction:
    return Transaction(
        transaction_id=txn_id,
        date=dt.date(2026, 10, 5),
        account="Chase",
        description="x",
        amount=Decimal(amount),
        category=category,
        pending=pending,
    )


class _PassThrough:
    """Keep the category a test set; categorization is tested elsewhere."""

    def categorize(self, transactions):
        from finance_sync.models import CategoryResult

        return [
            CategoryResult(t.transaction_id, t.category, t.category_source)
            for t in transactions
        ]


def keep(_overrides=None, _prior=None) -> _PassThrough:
    return _PassThrough()


def cell(rows: list[list[str]], line: str, column: str) -> str:
    row = next(r for r in rows if r[1] == line)
    return row[BUDGET_COLUMNS.index(column)]


class TestParseMoney:
    @pytest.mark.parametrize("raw,expected", [
        ("152", "152"), ("$1,355", "1355"), ("", "0"), ("junk", "0"),
        ("  42.50 ", "42.50"), ("(25)", "-25"),
    ])
    def test_tolerates_hand_entered_formats(self, raw, expected):
        assert parse_money(raw) == Decimal(expected)


class TestActuals:
    def test_spending_is_summed_as_a_positive_number(self):
        totals = actuals_by_category([txn(Category.GROCERIES, "-84.32")])
        assert totals[Category.GROCERIES] == Decimal("84.32")

    def test_income_does_not_inflate_a_spending_line(self):
        """A refund landing in groceries must not count as groceries spending."""
        totals = actuals_by_category([
            txn(Category.GROCERIES, "-100.00", "A"),
            txn(Category.GROCERIES, "25.00", "B"),
        ])
        assert totals[Category.GROCERIES] == Decimal("100.00")

    def test_pending_transactions_are_included(self):
        totals = actuals_by_category([txn(Category.STARBUCKS_COFFEE, "-7.45", pending=True)])
        assert totals[Category.STARBUCKS_COFFEE] == Decimal("7.45")

    def test_income_category_is_excluded_entirely(self):
        assert actuals_by_category([txn(Category.INCOME, "-3200.00")]) == {}

    def test_transfer_category_is_excluded_entirely(self):
        """A $5,100 withdrawal must not appear as spending."""
        assert actuals_by_category([txn(Category.TRANSFER, "-5100.00")]) == {}

    def test_non_budget_set_is_what_drives_the_exclusion(self):
        assert NON_BUDGET == {Category.INCOME, Category.TRANSFER}


class TestDefaultRows:
    def test_every_budget_line_is_present(self):
        rows = default_rows(now=NOW)
        assert [r[1] for r in rows[1:]] == [name for _, name, _ in BUDGET_LINES]

    def test_totals_row_comes_first(self):
        assert default_rows(now=NOW)[0][1] == "Totals"

    def test_planned_defaults_match_the_users_budget(self):
        rows = default_rows(now=NOW)
        assert cell(rows, "Rent and Insurance", "planned") == "1355"
        assert cell(rows, "Groceries", "planned") == "152"


class TestBuildRows:
    def test_totals_sum_the_lines(self):
        rows = build_rows([], now=NOW)
        planned = sum(Decimal(r[2]) for r in rows[1:])
        assert Decimal(rows[0][2]) == planned == Decimal("2016")

    def test_actual_appears_against_the_right_line(self):
        rows = build_rows([txn(Category.GROCERIES, "-198.00")], now=NOW)
        assert cell(rows, "Groceries", "actual") == "198.00"

    def test_difference_is_actual_minus_planned(self):
        rows = build_rows([txn(Category.GROCERIES, "-198.00")], now=NOW)
        assert cell(rows, "Groceries", "difference") == "46.00"

    def test_underspend_shows_negative(self):
        rows = build_rows([txn(Category.GROCERIES, "-52.00")], now=NOW)
        assert cell(rows, "Groceries", "difference") == "-100.00"

    def test_hand_edited_planned_amounts_are_preserved(self):
        """The planned column is the user's; a sync must never clobber it."""
        existing = build_rows([], now=NOW)[1:]
        for row in existing:
            if row[1] == "Groceries":
                row[2] = "300"
        rows = build_rows([], existing=existing, now=NOW)
        assert cell(rows, "Groceries", "planned") == "300.00"

    def test_edited_planned_flows_into_the_total(self):
        existing = build_rows([], now=NOW)[1:]
        for row in existing:
            if row[1] == "Groceries":
                row[2] = "252"
        rows = build_rows([], existing=existing, now=NOW)
        assert Decimal(rows[0][2]) == Decimal("2116")

    def test_a_line_with_no_spending_shows_zero(self):
        rows = build_rows([], now=NOW)
        assert cell(rows, "Pets", "actual") == "0.00"

    def test_transfers_do_not_reach_the_totals(self):
        rows = build_rows([txn(Category.TRANSFER, "-5100.00")], now=NOW)
        assert Decimal(rows[0][3]) == Decimal("0")


class TestReadPlanned:
    def test_unknown_category_rows_are_ignored(self):
        assert read_planned([["not_a_category", "X", "10"]]) == {}

    def test_header_row_is_ignored(self):
        assert read_planned([["category", "line", "planned"]]) == {}

    def test_totals_row_has_no_category_and_is_skipped(self):
        assert read_planned([["", "Totals", "2016"]]) == {}


class TestBudgetInTheSync:
    @pytest.fixture
    def backend(self):
        return FakeSheetsBackend()

    def test_a_new_spreadsheet_gets_a_populated_budget_tab(self, backend):
        SheetsSync(backend).sync([txn(Category.GROCERIES, "-198.00")], now=NOW, categorizer_factory=keep)
        rows = backend.tab("Finances 2026-10", "Budget")
        assert rows[0] == BUDGET_COLUMNS
        assert rows[1][1] == "Totals"
        assert cell(rows[1:], "Groceries", "actual") == "198.00"

    def test_actuals_update_on_a_later_sync(self, backend):
        sync = SheetsSync(backend)
        sync.sync([txn(Category.GROCERIES, "-100.00", "A")], now=NOW, categorizer_factory=keep)
        sync.sync([txn(Category.GROCERIES, "-98.00", "B")], now=NOW, categorizer_factory=keep)
        rows = backend.tab("Finances 2026-10", "Budget")[1:]
        assert cell(rows, "Groceries", "actual") == "198.00"

    def test_an_unchanged_run_does_not_rewrite_the_budget(self, backend):
        """Idempotency applies to the Budget tab too."""
        sync = SheetsSync(backend)
        sync.sync([txn(Category.GROCERIES, "-198.00")], now=NOW, categorizer_factory=keep)
        before = len(backend.write_calls())
        sync.sync([txn(Category.GROCERIES, "-198.00")], now=NOW, categorizer_factory=keep)
        assert len(backend.write_calls()) == before

    def test_hand_edited_planned_survives_a_sync(self, backend):
        sync = SheetsSync(backend)
        sync.sync([txn(Category.GROCERIES, "-198.00")], now=NOW, categorizer_factory=keep)
        rows = backend.tab("Finances 2026-10", "Budget")
        for row in rows[1:]:
            if row[1] == "Groceries":
                row[2] = "300.00"
        sync.sync([txn(Category.GROCERIES, "-198.00", "B2")], now=NOW, categorizer_factory=keep)
        rows = backend.tab("Finances 2026-10", "Budget")[1:]
        assert cell(rows, "Groceries", "planned") == "300.00"

    def test_planned_amounts_carry_into_a_new_month(self, backend):
        sync = SheetsSync(backend)
        sync.sync([txn(Category.GROCERIES, "-10.00", "A")], now=NOW, categorizer_factory=keep)
        rows = backend.tab("Finances 2026-10", "Budget")
        for row in rows[1:]:
            if row[1] == "Groceries":
                row[2] = "275.00"

        november = Transaction(
            transaction_id="N1", date=dt.date(2026, 11, 2), account="Chase",
            description="x", amount=Decimal("-5.00"), category=Category.GROCERIES)
        sync.sync([november], now=NOW, categorizer_factory=keep)

        rows = backend.tab("Finances 2026-11", "Budget")[1:]
        assert cell(rows, "Groceries", "planned") == "275.00"

    def test_actuals_do_not_carry_into_a_new_month(self, backend):
        """Planned carries forward; last month's spending must not."""
        sync = SheetsSync(backend)
        sync.sync([txn(Category.GROCERIES, "-198.00", "A")], now=NOW, categorizer_factory=keep)
        november = Transaction(
            transaction_id="N1", date=dt.date(2026, 11, 2), account="Chase",
            description="x", amount=Decimal("-5.00"), category=Category.GROCERIES)
        sync.sync([november], now=NOW, categorizer_factory=keep)
        rows = backend.tab("Finances 2026-11", "Budget")[1:]
        assert cell(rows, "Groceries", "actual") == "5.00"

    def test_a_dry_run_writes_no_budget(self, backend):
        SheetsSync(backend, dry_run=True).sync([txn(Category.GROCERIES, "-1.00")], now=NOW, categorizer_factory=keep)
        assert backend.write_calls() == []


class TestSchemaMigration:
    """Spreadsheets created by an earlier version lack the Budget tab."""

    @pytest.fixture
    def legacy(self) -> FakeSheetsBackend:
        backend = FakeSheetsBackend()
        folder = backend.ensure_folder("Finances")
        backend.create_spreadsheet(folder, "Finances 2026-10", {
            "Transactions": ["transaction_id"], "Review": ["transaction_id"],
            "Overrides": ["pattern"],
        })
        return backend

    def test_the_missing_budget_tab_is_added(self, legacy):
        SheetsSync(legacy).sync([txn(Category.GROCERIES, "-10.00")], now=NOW, categorizer_factory=keep)
        assert "Budget" in legacy.spreadsheets["sheet-1"]

    def test_the_added_tab_is_populated(self, legacy):
        SheetsSync(legacy).sync([txn(Category.GROCERIES, "-198.00")], now=NOW, categorizer_factory=keep)
        rows = legacy.tab("Finances 2026-10", "Budget")[1:]
        assert cell(rows, "Groceries", "actual") == "198.00"

    def test_existing_tabs_are_not_recreated(self, legacy):
        before = list(legacy.tab("Finances 2026-10", "Transactions"))
        SheetsSync(legacy).sync([txn(Category.GROCERIES, "-10.00")], now=NOW, categorizer_factory=keep)
        assert legacy.tab("Finances 2026-10", "Transactions")[0] == before[0]

    def test_a_dry_run_does_not_migrate(self, legacy):
        SheetsSync(legacy, dry_run=True).sync([txn(Category.GROCERIES, "-10.00")], now=NOW)
        assert "Budget" not in legacy.spreadsheets["sheet-1"]
