"""The guarantees this project exists for.

Running twice must not duplicate. Settling must update in place. A hand-edited
category must survive every subsequent sync.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from finance_sync.fake_backend import FakeSheetsBackend
from finance_sync.models import Category, CategorySource, Transaction
from finance_sync.rows import TRANSACTION_COLUMNS, row_to_transaction
from finance_sync.sheets import SheetsSync, spreadsheet_title
from finance_sync.simplefin import parse_accounts

NOW = dt.datetime(2026, 10, 9, 6, 0, tzinfo=dt.UTC)
LATER = dt.datetime(2026, 10, 14, 6, 0, tzinfo=dt.UTC)


@pytest.fixture
def backend() -> FakeSheetsBackend:
    return FakeSheetsBackend()


@pytest.fixture
def sync(backend) -> SheetsSync:
    return SheetsSync(backend, folder_name="Finances")


def txn(
    txn_id: str = "TXN-1",
    *,
    date: dt.date = dt.date(2026, 10, 5),
    amount: str = "-10.00",
    pending: bool = False,
    posted: dt.date | None = dt.date(2026, 10, 6),
    category: Category = Category.MISCL,
    source: CategorySource = CategorySource.NONE,
    description: str = "TEST MERCHANT",
) -> Transaction:
    return Transaction(
        transaction_id=txn_id,
        date=date,
        posted_date=None if pending else posted,
        account="My Bank Everyday Checking",
        description=description,
        amount=Decimal(amount),
        category=category,
        pending=pending,
        category_source=source,
    )


def column(row: list[str], name: str) -> str:
    return row[TRANSACTION_COLUMNS.index(name)]


class _PassThrough:
    """Keeps whatever category a test set, so upsert can be tested in isolation."""

    def categorize(self, transactions):
        from finance_sync.models import CategoryResult

        return [
            CategoryResult(t.transaction_id, t.category, t.category_source)
            for t in transactions
        ]


def keep_categories(_overrides=None) -> _PassThrough:
    return _PassThrough()


class TestSamePayloadTwice:
    def test_second_run_inserts_nothing(self, sync, backend):
        batch = [txn("TXN-1"), txn("TXN-2", amount="-20.00")]

        first = sync.sync(batch, now=NOW)
        second = sync.sync(batch, now=LATER)

        assert first["2026-10"].counts == {"inserted": 2, "updated": 0, "unchanged": 0}
        assert second["2026-10"].counts == {"inserted": 0, "updated": 0, "unchanged": 2}

    def test_row_count_is_stable_across_runs(self, sync, backend):
        batch = [txn("TXN-1"), txn("TXN-2", amount="-20.00")]
        sync.sync(batch, now=NOW)
        sync.sync(batch, now=LATER)
        sync.sync(batch, now=LATER)
        assert len(backend.data_rows(spreadsheet_title("2026-10"))) == 2

    def test_no_duplicate_transaction_ids(self, sync, backend):
        batch = [txn("TXN-1"), txn("TXN-2", amount="-20.00")]
        sync.sync(batch, now=NOW)
        sync.sync(batch, now=LATER)
        ids = [column(r, "transaction_id") for r in backend.data_rows(spreadsheet_title("2026-10"))]
        assert ids == sorted(set(ids))

    def test_unchanged_run_issues_no_writes(self, sync, backend):
        batch = [txn("TXN-1")]
        sync.sync(batch, now=NOW)
        before = len(backend.write_calls())
        sync.sync(batch, now=LATER)
        assert len(backend.write_calls()) == before

    def test_last_updated_is_not_churned_on_an_unchanged_row(self, sync, backend):
        """Stamping last_updated every run would make every row look changed."""
        batch = [txn("TXN-1")]
        sync.sync(batch, now=NOW)
        stamp_before = column(backend.data_rows(spreadsheet_title("2026-10"))[0], "last_updated")
        sync.sync(batch, now=LATER)
        stamp_after = column(backend.data_rows(spreadsheet_title("2026-10"))[0], "last_updated")
        assert stamp_before == stamp_after

    def test_duplicate_id_within_one_payload_inserts_once(self, sync, backend):
        sync.sync([txn("TXN-1"), txn("TXN-1", amount="-99.00")], now=NOW)
        assert len(backend.data_rows(spreadsheet_title("2026-10"))) == 1


class TestPendingToPosted:
    def test_settling_updates_in_place(self, sync, backend):
        pending = txn("TXN-1", pending=True, amount="-18.75")
        sync.sync([pending], now=NOW)

        settled = txn("TXN-1", pending=False, posted=dt.date(2026, 10, 13), amount="-18.75")
        plan = sync.sync([settled], now=LATER)

        assert plan["2026-10"].counts == {"inserted": 0, "updated": 1, "unchanged": 0}
        assert len(backend.data_rows(spreadsheet_title("2026-10"))) == 1

    def test_pending_flag_flips_to_false(self, sync, backend):
        sync.sync([txn("TXN-1", pending=True, amount="-18.75")], now=NOW)
        sync.sync([txn("TXN-1", pending=False, posted=dt.date(2026, 10, 13), amount="-18.75")], now=LATER)
        row = backend.data_rows(spreadsheet_title("2026-10"))[0]
        assert column(row, "pending") == "FALSE"

    def test_posted_date_is_populated_on_settle(self, sync, backend):
        sync.sync([txn("TXN-1", pending=True, amount="-18.75")], now=NOW)
        row_before = backend.data_rows(spreadsheet_title("2026-10"))[0]
        assert column(row_before, "posted_date") == ""

        sync.sync([txn("TXN-1", pending=False, posted=dt.date(2026, 10, 13), amount="-18.75")], now=LATER)
        row_after = backend.data_rows(spreadsheet_title("2026-10"))[0]
        assert column(row_after, "posted_date") == "2026-10-13"

    def test_last_updated_advances_when_something_changed(self, sync, backend):
        sync.sync([txn("TXN-1", pending=True, amount="-18.75")], now=NOW)
        before = column(backend.data_rows(spreadsheet_title("2026-10"))[0], "last_updated")
        sync.sync([txn("TXN-1", pending=False, posted=dt.date(2026, 10, 13), amount="-18.75")], now=LATER)
        after = column(backend.data_rows(spreadsheet_title("2026-10"))[0], "last_updated")
        assert after > before

    def test_settling_is_itself_idempotent(self, sync, backend):
        sync.sync([txn("TXN-1", pending=True, amount="-18.75")], now=NOW)
        settled = txn("TXN-1", pending=False, posted=dt.date(2026, 10, 13), amount="-18.75")
        sync.sync([settled], now=LATER)
        plan = sync.sync([settled], now=LATER)
        assert plan["2026-10"].counts["unchanged"] == 1
        assert len(backend.data_rows(spreadsheet_title("2026-10"))) == 1


class TestAmountChangeOnSettle:
    def test_amount_is_updated_in_place(self, sync, backend):
        sync.sync([txn("TXN-1", pending=True, amount="-18.75")], now=NOW)
        plan = sync.sync(
            [txn("TXN-1", pending=False, posted=dt.date(2026, 10, 13), amount="-21.25")],
            now=LATER,
        )
        rows = backend.data_rows(spreadsheet_title("2026-10"))
        assert plan["2026-10"].counts["updated"] == 1
        assert len(rows) == 1
        assert column(rows[0], "amount") == "-21.25"

    def test_amount_change_alone_counts_as_an_update(self, sync, backend):
        sync.sync([txn("TXN-1", amount="-10.00")], now=NOW)
        plan = sync.sync([txn("TXN-1", amount="-12.34")], now=LATER)
        assert plan["2026-10"].counts == {"inserted": 0, "updated": 1, "unchanged": 0}

    def test_trailing_zero_difference_is_not_a_change(self, sync, backend):
        """-18.75 and -18.750 are the same money; don't write a pointless update."""
        sync.sync([txn("TXN-1", amount="-18.75")], now=NOW)
        plan = sync.sync([txn("TXN-1", amount="-18.750")], now=LATER)
        assert plan["2026-10"].counts["unchanged"] == 1


class TestManualCategoryIsNeverOverwritten:
    def _hand_edit(self, backend, *, category="outside_food", source="manual"):
        """Simulate editing the sheet by hand."""
        rows = backend.data_rows(spreadsheet_title("2026-10"))
        rows[0][TRANSACTION_COLUMNS.index("category")] = category
        rows[0][TRANSACTION_COLUMNS.index("category_source")] = source

    def test_manual_category_survives_a_resync(self, sync, backend):
        sync.sync([txn("TXN-1", category=Category.MISCL, source=CategorySource.RULES)], now=NOW)
        self._hand_edit(backend)

        sync.sync([txn("TXN-1", category=Category.GROCERIES, source=CategorySource.RULES)], now=LATER)

        row = backend.data_rows(spreadsheet_title("2026-10"))[0]
        assert column(row, "category") == "outside_food"
        assert column(row, "category_source") == "manual"

    def test_manual_row_with_no_other_change_is_unchanged(self, sync, backend):
        sync.sync([txn("TXN-1", source=CategorySource.RULES)], now=NOW)
        self._hand_edit(backend)
        plan = sync.sync([txn("TXN-1", category=Category.GROCERIES, source=CategorySource.RULES)], now=LATER)
        assert plan["2026-10"].counts["unchanged"] == 1
        assert plan["2026-10"].preserved_manual == ["TXN-1"]

    def test_manual_category_survives_pending_to_posted(self, sync, backend):
        sync.sync([txn("TXN-1", pending=True, amount="-18.75", source=CategorySource.RULES)], now=NOW)
        self._hand_edit(backend, category="starbucks_coffee")

        sync.sync(
            [txn("TXN-1", pending=False, posted=dt.date(2026, 10, 13),
                 amount="-21.25", category=Category.GROCERIES, source=CategorySource.RULES)],
            now=LATER,
        )

        row = backend.data_rows(spreadsheet_title("2026-10"))[0]
        assert column(row, "category") == "starbucks_coffee"
        assert column(row, "category_source") == "manual"
        # ...while the financial fields still move.
        assert column(row, "amount") == "-21.25"
        assert column(row, "pending") == "FALSE"
        assert column(row, "posted_date") == "2026-10-13"

    def test_manual_category_survives_many_runs(self, sync, backend):
        sync.sync([txn("TXN-1", source=CategorySource.RULES)], now=NOW)
        self._hand_edit(backend, category="haircut")
        for _ in range(5):
            sync.sync([txn("TXN-1", category=Category.PERSONAL, source=CategorySource.RULES)], now=LATER)
        row = backend.data_rows(spreadsheet_title("2026-10"))[0]
        assert column(row, "category") == "haircut"

    def test_non_manual_category_is_allowed_to_change(self, sync, backend):
        """Only `manual` is protected; a rules result may be corrected."""
        sync.sync([txn("TXN-1", category=Category.MISCL, source=CategorySource.RULES)],
                  now=NOW, categorizer_factory=keep_categories)
        sync.sync([txn("TXN-1", category=Category.GROCERIES, source=CategorySource.RULES)],
                  now=LATER, categorizer_factory=keep_categories)
        row = backend.data_rows(spreadsheet_title("2026-10"))[0]
        assert column(row, "category") == "groceries"


class TestMonthRouting:
    def test_transaction_routes_by_its_own_date_not_the_run_date(self, sync, backend):
        """A run on the 1st must file the previous month's stragglers correctly."""
        straggler = txn("TXN-SEP", date=dt.date(2026, 9, 29), posted=dt.date(2026, 9, 30))
        current = txn("TXN-OCT", date=dt.date(2026, 10, 1), posted=dt.date(2026, 10, 2))

        sync.sync([straggler, current], now=dt.datetime(2026, 10, 1, 6, tzinfo=dt.UTC))

        assert [column(r, "transaction_id") for r in backend.data_rows("Finances 2026-09")] == ["TXN-SEP"]
        assert [column(r, "transaction_id") for r in backend.data_rows("Finances 2026-10")] == ["TXN-OCT"]

    def test_missing_spreadsheet_is_created(self, sync, backend):
        sync.sync([txn("TXN-1", date=dt.date(2026, 11, 3), posted=dt.date(2026, 11, 4))], now=NOW)
        assert "Finances 2026-11" in backend.created_spreadsheets

    def test_existing_spreadsheet_is_reused_not_recreated(self, sync, backend):
        sync.sync([txn("TXN-1")], now=NOW)
        sync.sync([txn("TXN-2", amount="-5.00")], now=LATER)
        assert backend.created_spreadsheets.count("Finances 2026-10") == 1

    def test_folder_is_created_once(self, sync, backend):
        sync.sync([txn("TXN-1")], now=NOW)
        sync.sync([txn("TXN-2", amount="-5.00")], now=LATER)
        assert list(backend.folders) == ["Finances"]

    def test_new_spreadsheet_has_all_four_tabs(self, sync, backend):
        sync.sync([txn("TXN-1")], now=NOW)
        sid = next(s for s, t in backend.titles.items() if t == "Finances 2026-10")
        assert set(backend.spreadsheets[sid]) == {
            "Transactions", "Budget", "Review", "Overrides"
        }

    def test_new_spreadsheet_has_a_header_row(self, sync, backend):
        sync.sync([txn("TXN-1")], now=NOW)
        assert backend.tab("Finances 2026-10", "Transactions")[0] == TRANSACTION_COLUMNS

    def test_cross_month_run_is_idempotent(self, sync, backend):
        batch = [
            txn("TXN-SEP", date=dt.date(2026, 9, 29), posted=dt.date(2026, 9, 30)),
            txn("TXN-OCT", date=dt.date(2026, 10, 1), posted=dt.date(2026, 10, 2)),
        ]
        sync.sync(batch, now=NOW)
        plans = sync.sync(batch, now=LATER)
        assert plans["2026-09"].counts["unchanged"] == 1
        assert plans["2026-10"].counts["unchanged"] == 1


class TestDryRun:
    def test_dry_run_writes_nothing(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        dry.sync([txn("TXN-1")], now=NOW)
        assert backend.write_calls() == []

    def test_dry_run_still_reports_what_would_change(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        plans = dry.sync([txn("TXN-1")], now=NOW)
        assert plans["2026-10"].counts["inserted"] == 1


class TestRoundTrip:
    def test_a_written_row_parses_back_to_the_same_transaction(self, sync, backend):
        original = txn("TXN-1", amount="-84.32", category=Category.GROCERIES, source=CategorySource.RULES)
        sync.sync([original], now=NOW, categorizer_factory=keep_categories)
        row = backend.data_rows(spreadsheet_title("2026-10"))[0]
        parsed = row_to_transaction(row)
        assert parsed is not None
        assert parsed.transaction_id == original.transaction_id
        assert parsed.amount == original.amount
        assert parsed.category == original.category
        assert parsed.pending == original.pending
        assert parsed.date == original.date

    def test_real_fixture_payload_round_trips_idempotently(self, sync, backend, accounts_payload):
        """End-to-end against the recorded SimpleFIN shape, not synthetic rows."""
        transactions = parse_accounts(accounts_payload).transactions
        first = sync.sync(transactions, now=NOW)
        second = sync.sync(transactions, now=LATER)

        assert sum(p.counts["inserted"] for p in first.values()) == 7
        assert sum(p.counts["inserted"] for p in second.values()) == 0
        assert sum(p.counts["unchanged"] for p in second.values()) == 7

    def test_fixture_settlement_updates_the_existing_row(
        self, sync, backend, accounts_payload, settled_payload
    ):
        sync.sync(parse_accounts(accounts_payload).transactions, now=NOW)
        rows_before = len(backend.data_rows(spreadsheet_title("2026-10")))

        plans = sync.sync(parse_accounts(settled_payload).transactions, now=LATER)

        rows_after = backend.data_rows(spreadsheet_title("2026-10"))
        assert plans["2026-10"].counts == {"inserted": 0, "updated": 1, "unchanged": 0}
        assert len(rows_after) == rows_before

        settled = next(r for r in rows_after if column(r, "transaction_id") == "TXN-1003")
        assert column(settled, "amount") == "-21.25"
        assert column(settled, "pending") == "FALSE"
        assert column(settled, "posted_date") == "2026-10-13"


class TestHandEditedSheetTolerance:
    def test_blank_rows_are_skipped(self, sync, backend):
        sync.sync([txn("TXN-1")], now=NOW)
        backend.tab(spreadsheet_title("2026-10"), "Transactions").append([])
        plan = sync.sync([txn("TXN-1")], now=LATER)
        assert plan["2026-10"].counts["unchanged"] == 1

    def test_a_short_row_does_not_crash_the_run(self, sync, backend):
        sync.sync([txn("TXN-1")], now=NOW)
        backend.tab(spreadsheet_title("2026-10"), "Transactions").append(["TXN-9", "2026-10-02"])
        plan = sync.sync([txn("TXN-1")], now=LATER)
        assert plan["2026-10"].counts["unchanged"] == 1

    def test_an_unknown_category_string_degrades_to_other(self, sync, backend):
        sync.sync([txn("TXN-1", source=CategorySource.RULES)], now=NOW)
        rows = backend.data_rows(spreadsheet_title("2026-10"))
        rows[0][TRANSACTION_COLUMNS.index("category")] = "brunch money"
        parsed = row_to_transaction(rows[0])
        assert parsed is not None and parsed.category == Category.MISCL

    def test_currency_formatted_amount_is_parsed(self):
        row = ["TXN-1", "2026-10-05", "", "acct", "desc", "$1,234.56"]
        parsed = row_to_transaction(row)
        assert parsed is not None and parsed.amount == Decimal("1234.56")


class TestOverridesSurviveMonthRollover:
    """Overrides are hand-maintained; a per-month tab must not reset them."""

    def test_overrides_are_carried_into_a_new_month(self, sync, backend):
        sync.sync([txn("TXN-SEP", date=dt.date(2026, 9, 10), posted=dt.date(2026, 9, 11))], now=NOW)
        backend.append_rows(
            next(s for s, t in backend.titles.items() if t == "Finances 2026-09"),
            "Overrides",
            [["TARGET", "groceries", "FALSE"], ["USC HOSPITALITY", "outside_food", "FALSE"]],
        )

        sync.sync([txn("TXN-OCT", date=dt.date(2026, 10, 2), posted=dt.date(2026, 10, 3))], now=NOW)

        carried = backend.tab("Finances 2026-10", "Overrides")[1:]
        assert [r[0] for r in carried] == ["TARGET", "USC HOSPITALITY"]

    def test_the_newest_previous_month_is_the_source(self, backend):
        s = SheetsSync(backend, folder_name="Finances")
        s.sync([txn("A", date=dt.date(2026, 8, 5), posted=dt.date(2026, 8, 6))], now=NOW)
        s.sync([txn("B", date=dt.date(2026, 9, 5), posted=dt.date(2026, 9, 6))], now=NOW)
        backend.append_rows(
            next(i for i, t in backend.titles.items() if t == "Finances 2026-08"),
            "Overrides", [["OLD", "miscl", "FALSE"]])
        backend.append_rows(
            next(i for i, t in backend.titles.items() if t == "Finances 2026-09"),
            "Overrides", [["NEW", "rent_and_insurance", "FALSE"]])

        s.sync([txn("C", date=dt.date(2026, 10, 5), posted=dt.date(2026, 10, 6))], now=NOW)
        carried = [r[0] for r in backend.tab("Finances 2026-10", "Overrides")[1:]]
        assert carried == ["NEW"]

    def test_a_later_month_is_not_used_as_the_source(self, backend):
        """Backfilling an older month must not inherit from a newer one."""
        s = SheetsSync(backend, folder_name="Finances")
        s.sync([txn("A", date=dt.date(2026, 10, 5), posted=dt.date(2026, 10, 6))], now=NOW)
        backend.append_rows(
            next(i for i, t in backend.titles.items() if t == "Finances 2026-10"),
            "Overrides", [["FUTURE", "miscl", "FALSE"]])

        s.sync([txn("B", date=dt.date(2026, 9, 5), posted=dt.date(2026, 9, 6))], now=NOW)
        assert backend.tab("Finances 2026-09", "Overrides")[1:] == []

    def test_no_previous_month_is_not_an_error(self, sync, backend):
        sync.sync([txn("TXN-1")], now=NOW)
        assert backend.tab("Finances 2026-10", "Overrides")[1:] == []

    def test_blank_override_rows_are_not_carried(self, sync, backend):
        sync.sync([txn("A", date=dt.date(2026, 9, 5), posted=dt.date(2026, 9, 6))], now=NOW)
        backend.append_rows(
            next(i for i, t in backend.titles.items() if t == "Finances 2026-09"),
            "Overrides", [["", "", ""], ["REAL", "miscl", "FALSE"]])
        sync.sync([txn("B", date=dt.date(2026, 10, 5), posted=dt.date(2026, 10, 6))], now=NOW)
        assert [r[0] for r in backend.tab("Finances 2026-10", "Overrides")[1:]] == ["REAL"]


class TestDryRunCreatesNothing:
    """A dry run that creates spreadsheets is not a dry run."""

    def test_no_spreadsheet_is_created(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        dry.sync([txn("TXN-1")], now=NOW)
        assert backend.created_spreadsheets == []

    def test_no_folder_write_occurs(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        dry.sync([txn("TXN-1")], now=NOW)
        assert backend.write_calls() == []

    def test_it_reports_that_it_would_create_the_spreadsheet(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        plans = dry.sync([txn("TXN-1")], now=NOW)
        assert plans["2026-10"].would_create is True
        assert "would be CREATED" in plans["2026-10"].describe()

    def test_it_still_plans_the_inserts(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        plans = dry.sync([txn("TXN-1"), txn("TXN-2", amount="-3.00")], now=NOW)
        assert plans["2026-10"].counts["inserted"] == 2

    def test_it_still_categorizes(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        plans = dry.sync([txn("TXN-1", description="TRADER JOE S #250")], now=NOW)
        row = plans["2026-10"].inserts[0]
        assert column(row, "category") == "groceries"

    def test_an_existing_spreadsheet_is_still_diffed_properly(self, backend):
        live = SheetsSync(backend)
        live.sync([txn("TXN-1")], now=NOW)
        dry = SheetsSync(backend, dry_run=True)
        plans = dry.sync([txn("TXN-1")], now=LATER, categorizer_factory=keep_categories)
        assert plans["2026-10"].counts["unchanged"] == 1
        assert plans["2026-10"].would_create is False


class TestDryRunDoesNotCreateTheFolder:
    """The first fix missed this: planning created the folder as a side effect."""

    def test_no_folder_is_created(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        dry.sync([txn("TXN-1")], now=NOW)
        assert backend.folders == {}

    def test_it_still_reports_the_full_plan(self, backend):
        dry = SheetsSync(backend, dry_run=True)
        plans = dry.sync([txn("TXN-1")], now=NOW)
        assert plans["2026-10"].counts["inserted"] == 1
        assert plans["2026-10"].would_create is True

    def test_an_existing_folder_is_reused_without_writing(self, backend):
        backend.ensure_folder("Finances")
        calls_before = len(backend.write_calls())
        dry = SheetsSync(backend, dry_run=True)
        dry.sync([txn("TXN-1")], now=NOW)
        assert list(backend.folders) == ["Finances"]
        assert len(backend.write_calls()) == calls_before

    def test_a_real_run_still_creates_the_folder(self, backend):
        SheetsSync(backend).sync([txn("TXN-1")], now=NOW)
        assert list(backend.folders) == ["Finances"]
