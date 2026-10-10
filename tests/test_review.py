"""The feedback loop: reviewed rows become manual facts plus a lasting rule."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from finance_sync.fake_backend import FakeSheetsBackend
from finance_sync.models import Category, CategorySource
from finance_sync.review import process_rows, suggest_pattern
from finance_sync.rows import REVIEW_COLUMNS, TRANSACTION_COLUMNS
from finance_sync.sheets import SheetsSync

NOW = dt.datetime(2026, 10, 9, 6, 0, tzinfo=dt.UTC)


def review_row(txn_id="R1", description="VERCEL INC. VERCEL.COM CA 09/18",
               amount="-41.11", proposed="miscl", confidence="0.7700",
               category="", date="2026-09-18") -> list[str]:
    values = {
        "transaction_id": txn_id, "date": date, "account": "CHASE COLLEGE (0291)",
        "description": description, "amount": amount,
        "proposed_category": proposed, "confidence": confidence,
        "category": category, "recurring": "0.4900", "business_expense": "0.6900",
        "last_updated": NOW.isoformat(),
    }
    return [values[c] for c in REVIEW_COLUMNS]


class TestSuggestPattern:
    @pytest.mark.parametrize("description,expected", [
        ("VERCEL INC. VERCEL.COM CA 09/18", "VERCEL"),
        ("UBER   *EATS", "UBER EATS"),
        ("TRADER JOE S #250", "TRADER JOE"),
        ("WEPAPRINTING*DEPOSIT", "WEPAPRINTING DEPOSIT"),
    ])
    def test_strips_bank_noise(self, description, expected):
        assert suggest_pattern(description) == expected

    def test_state_codes_and_dates_are_dropped(self):
        assert "CA" not in suggest_pattern("SOMEPLACE CA 09/18")

    def test_store_numbers_are_dropped(self):
        assert suggest_pattern("TARGET STORE T3217") == "TARGET STORE"

    def test_uber_eats_stays_distinct_from_uber(self):
        """Collapsing these would send every Uber Eats charge to travel."""
        assert suggest_pattern("UBER   *EATS") != suggest_pattern("UBER   *TRIP")

    def test_payee_is_preferred_when_given(self):
        assert suggest_pattern("TRADER JOE S #250", payee="Trader Joe's") == "TRADER JOE"

    def test_an_underivable_description_yields_no_pattern(self):
        """Better no override than a meaningless one that matches everything."""
        assert suggest_pattern("###") == ""

    def test_no_override_is_generated_without_a_pattern(self):
        outcome = process_rows([review_row(description="###", category="gpt")], [])
        assert outcome.new_overrides == []
        assert len(outcome.promoted) == 1


class TestProcessRows:
    def test_a_blank_category_stays_pending(self):
        outcome = process_rows([review_row(category="")], [])
        assert outcome.promoted == []
        assert len(outcome.remaining) == 1

    def test_a_filled_category_is_promoted(self):
        outcome = process_rows([review_row(category="gpt")], [])
        assert len(outcome.promoted) == 1
        assert outcome.promoted[0].category == Category.GPT

    def test_promoted_rows_are_marked_manual(self):
        """Manual is what protects the edit from every future sync."""
        outcome = process_rows([review_row(category="gpt")], [])
        assert outcome.promoted[0].category_source == CategorySource.MANUAL

    def test_financial_fields_survive_the_move(self):
        outcome = process_rows([review_row(category="gpt", amount="-41.11")], [])
        txn = outcome.promoted[0]
        assert txn.amount == Decimal("-41.11")
        assert txn.date == dt.date(2026, 9, 18)

    def test_jev_extras_are_carried_over(self):
        outcome = process_rows([review_row(category="gpt")], [])
        assert outcome.promoted[0].recurring == 0.49
        assert outcome.promoted[0].business_expense == 0.69

    def test_an_override_is_generated(self):
        outcome = process_rows([review_row(category="gpt")], [])
        assert outcome.new_overrides == [["VERCEL", "gpt", "FALSE"]]

    def test_an_existing_override_is_not_duplicated(self):
        outcome = process_rows([review_row(category="gpt")], [["VERCEL", "miscl", "FALSE"]])
        assert outcome.new_overrides == []
        assert len(outcome.promoted) == 1

    def test_duplicate_merchants_in_one_pass_add_one_override(self):
        rows = [review_row("R1", category="gpt"), review_row("R2", category="gpt")]
        outcome = process_rows(rows, [])
        assert len(outcome.new_overrides) == 1
        assert len(outcome.promoted) == 2

    def test_an_unknown_category_is_skipped_and_kept(self):
        outcome = process_rows([review_row(category="brunch")], [])
        assert outcome.promoted == []
        assert outcome.skipped == ["R1"]
        assert len(outcome.remaining) == 1

    def test_an_unusable_date_is_skipped_not_guessed(self):
        outcome = process_rows([review_row(category="gpt", date="not-a-date")], [])
        assert outcome.skipped == ["R1"]

    def test_blank_rows_are_ignored(self):
        assert process_rows([[], ["", "", ""]], []).counts["promoted"] == 0

    def test_header_row_is_ignored(self):
        assert process_rows([REVIEW_COLUMNS], []).counts["promoted"] == 0

    def test_mixed_rows_split_correctly(self):
        rows = [
            review_row("R1", category="gpt"),
            review_row("R2", description="WEPAPRINTING*DEPOSIT", category=""),
        ]
        outcome = process_rows(rows, [])
        assert outcome.counts == {
            "promoted": 1, "overrides_added": 1, "still_pending": 1, "skipped": 0
        }


class TestApplyReview:
    @pytest.fixture
    def backend(self) -> FakeSheetsBackend:
        backend = FakeSheetsBackend()
        sync = SheetsSync(backend)
        sync.spreadsheet_for("2026-09")
        sid = next(s for s, t in backend.titles.items() if t == "Finances 2026-09")
        backend.append_rows(sid, "Review", [review_row(category="gpt"),
                                            review_row("R2", description="WEPAPRINTING*DEPOSIT")])
        return backend

    def _apply(self, backend):
        sync = SheetsSync(backend)
        rows = sync.review_rows("2026-09")
        outcome = process_rows(rows, sync.override_rows("2026-09"))
        sync.apply_review("2026-09", outcome)
        return outcome

    def test_promoted_row_lands_in_transactions(self, backend):
        self._apply(backend)
        rows = backend.data_rows("Finances 2026-09")
        assert len(rows) == 1
        assert rows[0][TRANSACTION_COLUMNS.index("category")] == "gpt"
        assert rows[0][TRANSACTION_COLUMNS.index("category_source")] == "manual"

    def test_the_override_is_written(self, backend):
        self._apply(backend)
        overrides = backend.tab("Finances 2026-09", "Overrides")[1:]
        assert overrides == [["VERCEL", "gpt", "FALSE"]]

    def test_the_reviewed_row_leaves_the_review_tab(self, backend):
        self._apply(backend)
        remaining = backend.tab("Finances 2026-09", "Review")[1:]
        assert [r[0] for r in remaining] == ["R2"]

    def test_the_header_survives_the_rewrite(self, backend):
        self._apply(backend)
        assert backend.tab("Finances 2026-09", "Review")[0] == REVIEW_COLUMNS

    def test_running_review_twice_is_safe(self, backend):
        self._apply(backend)
        self._apply(backend)
        assert len(backend.data_rows("Finances 2026-09")) == 1
        assert backend.tab("Finances 2026-09", "Overrides")[1:] == [["VERCEL", "gpt", "FALSE"]]

    def test_the_budget_is_refreshed_after_promotion(self, backend):
        self._apply(backend)
        from finance_sync.budget import BUDGET_COLUMNS

        rows = backend.tab("Finances 2026-09", "Budget")[1:]
        gpt = next(r for r in rows if r[1] == "GPT")
        assert gpt[BUDGET_COLUMNS.index("actual")] == "41.11"

    def test_a_dry_run_changes_nothing(self, backend):
        sync = SheetsSync(backend, dry_run=True)
        outcome = process_rows(sync.review_rows("2026-09"), [])
        before = len(backend.tab("Finances 2026-09", "Review")[1:])
        sync.apply_review("2026-09", outcome)
        assert len(backend.tab("Finances 2026-09", "Review")[1:]) == before
        assert backend.data_rows("Finances 2026-09") == []


class TestTheLoopCloses:
    def test_the_override_stops_the_merchant_being_asked_about_again(self):
        """The whole point: review once, never again."""
        from finance_sync.categorize import LayeredCategorizer, parse_overrides
        from finance_sync.models import Transaction

        outcome = process_rows([review_row(category="gpt")], [])
        overrides = parse_overrides(outcome.new_overrides)

        future = Transaction(
            transaction_id="R9", date=dt.date(2026, 11, 18), account="CHASE",
            description="VERCEL INC. VERCEL.COM CA 11/18", amount=Decimal("-41.11"),
            payee="Vercel.com",
        )
        result = LayeredCategorizer(overrides=overrides).categorize([future])[0]
        assert result.category == Category.GPT
        assert result.source == CategorySource.OVERRIDE
