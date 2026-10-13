"""Jev layer: gating, degradation, and that it only sees what it should."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from finance_sync.categorize import LayeredCategorizer, NullCategorizer
from finance_sync.jev import (
    CATEGORY_CRITERIA,
    JevCategorizer,
    build_categorizer,
    build_state,
)
from finance_sync.models import Category, CategorySource, Transaction


def txn(payee="Mystery Merchant", description="MYSTERY 123", amount="-12.00",
        txn_id="T1") -> Transaction:
    return Transaction(
        transaction_id=txn_id, date=dt.date(2026, 10, 5),
        account="Chase Freedom Unlimited (5047)", description=description,
        payee=payee, amount=Decimal(amount),
    )


class FakeChoice:
    type = "choice"

    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence
        self.probabilities = {choice: confidence}


class FakeNoul:
    type = "noul"

    def __init__(self, noul):
        self.noul = noul


class FakeResponse:
    def __init__(self, answers):
        self.answers = answers
        self.model = "jev-1.13.0"


class FakeClient:
    """Records calls and returns scripted answers."""

    def __init__(self, answers=None, raises=None):
        self.answers = answers or {
            "category": FakeChoice("outside_food", 0.93),
            "recurring": FakeNoul(0.04),
            "business": FakeNoul(0.11),
        }
        self.raises = raises
        self.calls = []

    def system_one(self, state, questions, model=None):
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self.raises:
            raise self.raises
        return FakeResponse(self.answers)


class TestState:
    def test_state_uses_named_fields(self):
        state = build_state(txn())
        assert set(state) == {
            "merchant", "bank_description", "amount_usd", "direction",
            "account", "date",
        }

    def test_amount_is_absolute_with_an_explicit_direction(self):
        state = build_state(txn(amount="-12.00"))
        assert state["amount_usd"] == "12.00"
        assert state["direction"] == "money_out"

    def test_money_in_is_marked(self):
        assert build_state(txn(amount="3200.00"))["direction"] == "money_in"

    def test_merchant_prefers_payee(self):
        state = build_state(txn(payee="Tamix", description="TAMIX ON HOOVER 323"))
        assert state["merchant"] == "Tamix"


class TestCriteria:
    def test_every_criterion_is_a_real_category(self):
        for key in CATEGORY_CRITERIA:
            Category(key)  # raises if invalid

    def test_sk_is_not_offered_to_jev(self):
        """SK is a manual-only budget line."""
        assert "sk" not in CATEGORY_CRITERIA

    def test_every_budget_line_except_sk_is_offered(self):
        from finance_sync.models import BUDGET_LINES

        offered = set(CATEGORY_CRITERIA)
        expected = {c.value for c, _, _ in BUDGET_LINES} - {"sk"}
        assert expected <= offered


class TestConfidenceGating:
    def test_high_confidence_is_written_straight_through(self):
        client = FakeClient()
        jev = JevCategorizer("k", confidence_threshold=0.8, client=client)
        result = jev.categorize([txn()])[0]
        assert result.category == Category.OUTSIDE_FOOD
        assert result.source == CategorySource.JEV
        assert result.confidence == 0.93
        assert result.needs_review is False

    def test_low_confidence_is_routed_to_review(self):
        client = FakeClient({"category": FakeChoice("personal", 0.42)})
        jev = JevCategorizer("k", confidence_threshold=0.8, client=client)
        result = jev.categorize([txn()])[0]
        assert result.needs_review is True
        assert result.category == Category.PERSONAL
        assert result.confidence == 0.42

    def test_exactly_at_the_threshold_is_accepted(self):
        client = FakeClient({"category": FakeChoice("personal", 0.8)})
        jev = JevCategorizer("k", confidence_threshold=0.8, client=client)
        assert jev.categorize([txn()])[0].needs_review is False

    def test_the_threshold_is_configurable(self):
        client = FakeClient({"category": FakeChoice("personal", 0.5)})
        assert JevCategorizer("k", 0.9, client=client).categorize([txn()])[0].needs_review
        assert not JevCategorizer("k", 0.4, client=client).categorize([txn()])[0].needs_review


class TestExtraQuestions:
    def test_recurring_and_business_probabilities_are_captured(self):
        client = FakeClient({
            "category": FakeChoice("personal", 0.95),
            "recurring": FakeNoul(0.97),
            "business": FakeNoul(0.12),
        })
        result = JevCategorizer("k", client=client).categorize([txn()])[0]
        assert result.recurring == 0.97
        assert result.business_expense == 0.12

    def test_all_three_questions_go_in_one_call(self):
        """Parallel evaluation is the point; three calls would waste it."""
        client = FakeClient()
        JevCategorizer("k", client=client).categorize([txn()])
        assert len(client.calls) == 1
        assert set(client.calls[0]["questions"]) == {"category", "recurring", "business"}

    def test_missing_noul_answers_are_tolerated(self):
        client = FakeClient({"category": FakeChoice("personal", 0.95)})
        result = JevCategorizer("k", client=client).categorize([txn()])[0]
        assert result.recurring is None and result.business_expense is None


class TestDegradation:
    def test_an_api_failure_does_not_raise(self):
        """A categorization outage must not stop financial data reaching the sheet."""
        client = FakeClient(raises=RuntimeError("503"))
        result = JevCategorizer("k", client=client).categorize([txn()])[0]
        assert result.category == Category.MISCL
        assert result.source == CategorySource.NONE
        assert result.needs_review is False

    def test_an_unknown_category_degrades(self):
        client = FakeClient({"category": FakeChoice("crypto", 0.99)})
        result = JevCategorizer("k", client=client).categorize([txn()])[0]
        assert result.source == CategorySource.NONE

    def test_a_missing_category_answer_degrades(self):
        client = FakeClient({"recurring": FakeNoul(0.5)})
        assert JevCategorizer("k", client=client).categorize([txn()])[0].source == CategorySource.NONE

    def test_a_missing_confidence_is_treated_as_zero(self):
        class NoConfidence:
            choice = "personal"
            confidence = None
        client = FakeClient({"category": NoConfidence()})
        result = JevCategorizer("k", confidence_threshold=0.8, client=client).categorize([txn()])[0]
        assert result.needs_review is True

    def test_empty_input_makes_no_calls(self):
        client = FakeClient()
        assert JevCategorizer("k", client=client).categorize([]) == []
        assert client.calls == []

    def test_the_batch_cap_leaves_the_remainder_unresolved(self):
        client = FakeClient()
        jev = JevCategorizer("k", client=client, max_transactions=2)
        batch = [txn(txn_id=f"T{i}") for i in range(5)]
        results = jev.categorize(batch)
        assert len(client.calls) == 2
        assert len(results) == 5
        assert all(r.source == CategorySource.NONE for r in results[2:])


class TestLayering:
    def test_jev_only_sees_what_rules_could_not_resolve(self):
        client = FakeClient()
        jev = JevCategorizer("k", client=client)
        cat = LayeredCategorizer(fallback=jev)
        cat.categorize([
            txn(payee="Trader Joe's", description="TRADER JOE S #250", txn_id="A"),
            txn(payee="Blorp Unknown Co", description="BLORP 991", txn_id="B"),
        ])
        merchants = [c["state"]["merchant"] for c in client.calls]
        assert merchants == ["Blorp Unknown Co"]

    def test_overrides_still_beat_jev(self):
        from finance_sync.categorize import Override

        client = FakeClient()
        cat = LayeredCategorizer(
            overrides=[Override("MYSTERY", Category.PETS)],
            fallback=JevCategorizer("k", client=client),
        )
        result = cat.categorize([txn()])[0]
        assert result.category == Category.PETS
        assert client.calls == []


class TestBuildCategorizer:
    class Cfg:
        def __init__(self, use_jev, key, threshold=0.8):
            self.use_jev = use_jev
            self.typesafe_api_key = key
            self.jev_confidence_threshold = threshold

    def test_disabled_returns_the_null_categorizer(self):
        assert isinstance(build_categorizer(self.Cfg(False, "k")), NullCategorizer)

    def test_enabled_without_a_key_falls_back_safely(self):
        assert isinstance(build_categorizer(self.Cfg(True, "")), NullCategorizer)

    def test_enabled_with_a_key_returns_jev(self):
        assert isinstance(build_categorizer(self.Cfg(True, "k")), JevCategorizer)

    def test_the_threshold_is_passed_through(self):
        jev = build_categorizer(self.Cfg(True, "k", 0.95))
        assert jev._threshold == 0.95


class TestReviewRouting:
    """Where a low-confidence proposal actually lands."""

    @pytest.fixture
    def backend(self):
        from finance_sync.fake_backend import FakeSheetsBackend

        return FakeSheetsBackend()

    def _sync(self, backend, confidence, transactions):
        from finance_sync.sheets import SheetsSync

        client = FakeClient({"category": FakeChoice("personal", confidence)})
        factory = lambda overrides: LayeredCategorizer(  # noqa: E731
            overrides=overrides, fallback=JevCategorizer("k", 0.8, client=client)
        )
        return SheetsSync(backend).sync(
            transactions, now=dt.datetime(2026, 10, 9, tzinfo=dt.UTC),
            categorizer_factory=factory,
        )

    def test_high_confidence_lands_in_transactions(self, backend):
        plans = self._sync(backend, 0.95, [txn()])
        assert plans["2026-10"].counts["inserted"] == 1
        assert backend.tab("Finances 2026-10", "Review")[1:] == []

    def test_low_confidence_goes_to_review_not_transactions(self, backend):
        plans = self._sync(backend, 0.30, [txn()])
        assert plans["2026-10"].counts["inserted"] == 0
        assert plans["2026-10"].counts["routed_to_review"] == 1
        assert backend.data_rows("Finances 2026-10") == []
        assert len(backend.tab("Finances 2026-10", "Review")[1:]) == 1

    def test_the_review_row_carries_the_proposal_and_score(self, backend):
        from finance_sync.rows import REVIEW_COLUMNS

        self._sync(backend, 0.30, [txn()])
        row = backend.tab("Finances 2026-10", "Review")[1]
        assert row[REVIEW_COLUMNS.index("proposed_category")] == "personal"
        assert row[REVIEW_COLUMNS.index("confidence")] == "0.3000"

    def test_the_review_row_leaves_category_blank_for_the_human(self, backend):
        from finance_sync.rows import REVIEW_COLUMNS

        self._sync(backend, 0.30, [txn()])
        row = backend.tab("Finances 2026-10", "Review")[1]
        assert row[REVIEW_COLUMNS.index("category")] == ""

    def test_a_transaction_is_not_listed_in_review_twice(self, backend):
        self._sync(backend, 0.30, [txn()])
        self._sync(backend, 0.30, [txn()])
        assert len(backend.tab("Finances 2026-10", "Review")[1:]) == 1

    def test_an_already_written_row_keeps_its_category(self, backend):
        """A low-confidence guess must not overwrite a row already on the sheet."""
        from finance_sync.rows import TRANSACTION_COLUMNS

        self._sync(backend, 0.95, [txn()])
        before = backend.data_rows("Finances 2026-10")[0]
        assert before[TRANSACTION_COLUMNS.index("category")] == "personal"

        self._sync(backend, 0.20, [txn(amount="-99.00")])
        after = backend.data_rows("Finances 2026-10")[0]
        assert after[TRANSACTION_COLUMNS.index("category")] == "personal"
        assert after[TRANSACTION_COLUMNS.index("amount")] == "-99.00"


class TestScoreJitterDoesNotChurn:
    """Jev is probabilistic: the same question returns slightly different
    numbers each time. Those must not rewrite the row on every run."""

    @pytest.fixture
    def backend(self):
        from finance_sync.fake_backend import FakeSheetsBackend

        return FakeSheetsBackend()

    def _sync(self, backend, confidence, recurring=0.5):
        from finance_sync.sheets import SheetsSync

        client = FakeClient({
            "category": FakeChoice("personal", confidence),
            "recurring": FakeNoul(recurring),
        })
        factory = lambda o: LayeredCategorizer(  # noqa: E731
            overrides=o, fallback=JevCategorizer("k", 0.8, client=client))
        return SheetsSync(backend).sync(
            [txn()], now=dt.datetime(2026, 10, 9, tzinfo=dt.UTC),
            categorizer_factory=factory)

    def test_a_drifting_confidence_is_not_an_update(self):
        from finance_sync.fake_backend import FakeSheetsBackend

        backend = FakeSheetsBackend()
        self._sync(backend, 0.82)
        plans = self._sync(backend, 0.84)
        assert plans["2026-10"].counts["unchanged"] == 1
        assert plans["2026-10"].counts["updated"] == 0

    def test_a_drifting_noul_is_not_an_update(self):
        from finance_sync.fake_backend import FakeSheetsBackend

        backend = FakeSheetsBackend()
        self._sync(backend, 0.90, recurring=0.41)
        plans = self._sync(backend, 0.90, recurring=0.47)
        assert plans["2026-10"].counts["unchanged"] == 1

    def test_repeated_runs_issue_no_writes(self):
        from finance_sync.fake_backend import FakeSheetsBackend

        backend = FakeSheetsBackend()
        self._sync(backend, 0.81)
        before = len(backend.write_calls())
        for score in (0.83, 0.79, 0.85):
            self._sync(backend, score)
        assert len(backend.write_calls()) == before

    def test_an_actual_category_change_still_updates(self):
        """Suppressing jitter must not suppress real decisions."""
        from finance_sync.fake_backend import FakeSheetsBackend
        from finance_sync.sheets import SheetsSync

        backend = FakeSheetsBackend()
        self._sync(backend, 0.90)
        client = FakeClient({"category": FakeChoice("pets", 0.90)})
        factory = lambda o: LayeredCategorizer(  # noqa: E731
            overrides=o, fallback=JevCategorizer("k", 0.8, client=client))
        plans = SheetsSync(backend).sync(
            [txn()], now=dt.datetime(2026, 10, 9, tzinfo=dt.UTC),
            categorizer_factory=factory)
        assert plans["2026-10"].counts["updated"] == 1


class TestReviewIsPruned:
    """A borderline transaction can cross the threshold on a later run."""

    @pytest.fixture
    def backend(self):
        from finance_sync.fake_backend import FakeSheetsBackend

        return FakeSheetsBackend()

    def _sync(self, backend, confidence):
        from finance_sync.sheets import SheetsSync

        client = FakeClient({"category": FakeChoice("personal", confidence)})
        factory = lambda o: LayeredCategorizer(  # noqa: E731
            overrides=o, fallback=JevCategorizer("k", 0.8, client=client))
        return SheetsSync(backend).sync(
            [txn()], now=dt.datetime(2026, 10, 9, tzinfo=dt.UTC),
            categorizer_factory=factory)

    def test_crossing_the_threshold_clears_the_review_row(self, backend):
        self._sync(backend, 0.70)
        assert len(backend.tab("Finances 2026-10", "Review")[1:]) == 1

        self._sync(backend, 0.85)
        assert backend.data_rows("Finances 2026-10") != []
        assert backend.tab("Finances 2026-10", "Review")[1:] == []

    def test_nothing_is_ever_in_both_places(self, backend):
        from finance_sync.rows import row_to_transaction

        self._sync(backend, 0.70)
        self._sync(backend, 0.85)
        tx_ids = {
            t.transaction_id
            for r in backend.data_rows("Finances 2026-10")
            if (t := row_to_transaction(r))
        }
        review_ids = {r[0] for r in backend.tab("Finances 2026-10", "Review")[1:]}
        assert tx_ids & review_ids == set()

    def test_an_unresolved_row_stays_on_review(self, backend):
        self._sync(backend, 0.70)
        self._sync(backend, 0.72)
        assert len(backend.tab("Finances 2026-10", "Review")[1:]) == 1

    def test_the_review_header_survives_pruning(self, backend):
        from finance_sync.rows import REVIEW_COLUMNS

        self._sync(backend, 0.70)
        self._sync(backend, 0.85)
        assert backend.tab("Finances 2026-10", "Review")[0] == REVIEW_COLUMNS
