"""Jev must not be re-asked about transactions it already decided.

Stopping the row rewrites was not enough: the API call still happened on every
run, which is both wasted spend and the reason the scores drifted at all.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from finance_sync.categorize import LayeredCategorizer, Override
from finance_sync.fake_backend import FakeSheetsBackend
from finance_sync.jev import JevCategorizer
from finance_sync.models import Category, CategoryResult, CategorySource, Transaction
from finance_sync.sheets import SheetsSync
from tests.test_jev import FakeChoice, FakeClient, FakeNoul

NOW = dt.datetime(2026, 10, 9, 6, 0, tzinfo=dt.UTC)


def txn(txn_id="T1", payee="Blorp Unknown Co", description="BLORP 991",
        amount="-12.00") -> Transaction:
    return Transaction(
        transaction_id=txn_id, date=dt.date(2026, 10, 5),
        account="Chase Freedom Unlimited (5047)", description=description,
        payee=payee, amount=Decimal(amount),
    )


def prior(txn_id="T1", category=Category.PERSONAL, confidence=0.91,
          recurring=0.33, business=0.77) -> dict[str, CategoryResult]:
    return {
        txn_id: CategoryResult(
            transaction_id=txn_id, category=category, source=CategorySource.JEV,
            confidence=confidence, recurring=recurring, business_expense=business,
        )
    }


class TestCacheSkipsTheApiCall:
    def test_a_cached_decision_makes_no_api_call(self):
        client = FakeClient()
        cat = LayeredCategorizer(
            prior=prior(), fallback=JevCategorizer("k", client=client)
        )
        cat.categorize([txn()])
        assert client.calls == []

    def test_the_cached_category_is_returned(self):
        client = FakeClient()
        cat = LayeredCategorizer(
            prior=prior(category=Category.PETS), fallback=JevCategorizer("k", client=client)
        )
        result = cat.categorize([txn()])[0]
        assert result.category == Category.PETS
        assert result.source == CategorySource.JEV

    def test_the_cached_scores_are_returned_unchanged(self):
        """Reusing the stored scores is what stops the drift at source."""
        client = FakeClient()
        cat = LayeredCategorizer(
            prior=prior(confidence=0.8231, recurring=0.1234, business=0.5678),
            fallback=JevCategorizer("k", client=client),
        )
        result = cat.categorize([txn()])[0]
        assert result.confidence == 0.8231
        assert result.recurring == 0.1234
        assert result.business_expense == 0.5678

    def test_an_uncached_transaction_still_calls_jev_once(self):
        client = FakeClient()
        cat = LayeredCategorizer(prior={}, fallback=JevCategorizer("k", client=client))
        cat.categorize([txn()])
        assert len(client.calls) == 1

    def test_only_the_uncached_ones_are_asked(self):
        client = FakeClient()
        cat = LayeredCategorizer(
            prior=prior("T1"), fallback=JevCategorizer("k", client=client)
        )
        cat.categorize([txn("T1"), txn("T2")])
        assert len(client.calls) == 1

    def test_a_cached_low_confidence_answer_still_needs_review(self):
        client = FakeClient()
        cached = prior(confidence=0.40)
        cached["T1"] = CategoryResult(
            "T1", Category.PERSONAL, CategorySource.JEV,
            confidence=0.40, needs_review=True,
        )
        cat = LayeredCategorizer(prior=cached, fallback=JevCategorizer("k", client=client))
        assert cat.categorize([txn()])[0].needs_review is True
        assert client.calls == []


class TestCacheWinsOverLaterRules:
    """The chosen semantics: a cached Jev answer is final."""

    def test_a_new_override_does_not_change_a_cached_decision(self):
        client = FakeClient()
        cat = LayeredCategorizer(
            overrides=[Override("BLORP", Category.PETS)],
            prior=prior(category=Category.PERSONAL),
            fallback=JevCategorizer("k", client=client),
        )
        result = cat.categorize([txn()])[0]
        assert result.category == Category.PERSONAL
        assert result.source == CategorySource.JEV

    def test_an_override_still_governs_an_uncached_transaction(self):
        client = FakeClient()
        cat = LayeredCategorizer(
            overrides=[Override("BLORP", Category.PETS)],
            prior={},
            fallback=JevCategorizer("k", client=client),
        )
        result = cat.categorize([txn()])[0]
        assert result.category == Category.PETS
        assert result.source == CategorySource.OVERRIDE
        assert client.calls == []


class TestCacheIsBuiltFromTheSheet:
    @pytest.fixture
    def backend(self) -> FakeSheetsBackend:
        return FakeSheetsBackend()

    def _factory(self, client):
        def make(overrides, prior=None):
            return LayeredCategorizer(
                overrides=overrides, prior=prior,
                fallback=JevCategorizer("k", 0.8, client=client),
            )
        return make

    def test_a_second_sync_makes_no_api_calls(self, backend):
        client = FakeClient({"category": FakeChoice("personal", 0.93),
                             "recurring": FakeNoul(0.2), "business": FakeNoul(0.3)})
        sync = SheetsSync(backend)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) == 1

        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) == 1

    def test_the_second_sync_reports_unchanged(self, backend):
        client = FakeClient()
        sync = SheetsSync(backend)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        plans = sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert plans["2026-10"].counts["unchanged"] == 1

    def test_a_review_listed_transaction_is_not_reasked(self, backend):
        """Parked awaiting a human is still a decision already paid for."""
        client = FakeClient({"category": FakeChoice("personal", 0.35)})
        sync = SheetsSync(backend)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) == 1
        assert len(backend.tab("Finances 2026-10", "Review")[1:]) == 1

        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) == 1

    def test_a_new_transaction_is_still_asked_about(self, backend):
        client = FakeClient()
        sync = SheetsSync(backend)
        sync.sync([txn("T1")], now=NOW, categorizer_factory=self._factory(client))
        sync.sync([txn("T1"), txn("T2")], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) == 2

    def test_a_rules_resolvable_transaction_never_reaches_jev(self, backend):
        client = FakeClient()
        SheetsSync(backend).sync(
            [txn(payee="Trader Joe's", description="TRADER JOE S #250")],
            now=NOW, categorizer_factory=self._factory(client),
        )
        assert client.calls == []

    def test_a_manual_row_is_never_asked_about(self, backend):
        from finance_sync.rows import TRANSACTION_COLUMNS

        client = FakeClient()
        sync = SheetsSync(backend)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        rows = backend.data_rows("Finances 2026-10")
        rows[0][TRANSACTION_COLUMNS.index("category_source")] = "manual"
        rows[0][TRANSACTION_COLUMNS.index("category")] = "pets"

        calls_before = len(client.calls)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) == calls_before


class TestUnresolvedRowsDoNotLoop:
    """A low-confidence transaction already on the sheet must not be re-asked
    forever. It is unresolved, so it belongs on Review where a human can act
    and where the next run reads it back as a prior decision."""

    @pytest.fixture
    def backend(self) -> FakeSheetsBackend:
        return FakeSheetsBackend()

    def _factory(self, client):
        def make(overrides, prior=None):
            return LayeredCategorizer(
                overrides=overrides, prior=prior,
                fallback=JevCategorizer("k", 0.8, client=client),
            )
        return make

    def _seed_unresolved(self, backend, client):
        """First sync writes the row with source `none` (low confidence)."""
        sync = SheetsSync(backend)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        return sync

    def test_a_low_confidence_transaction_reaches_review(self, backend):
        client = FakeClient({"category": FakeChoice("personal", 0.35)})
        self._seed_unresolved(backend, client)
        assert len(backend.tab("Finances 2026-10", "Review")[1:]) == 1

    def test_it_is_not_reasked_on_the_next_run(self, backend):
        client = FakeClient({"category": FakeChoice("personal", 0.35)})
        sync = self._seed_unresolved(backend, client)
        calls = len(client.calls)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) == calls

    def test_a_jev_failure_does_not_loop_either(self, backend):
        """An API error yields source `none`; that must not retry forever."""
        client = FakeClient(raises=RuntimeError("503"))
        sync = SheetsSync(backend)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        first = len(client.calls)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) <= first + 1

    def test_a_decided_row_is_pruned_from_review(self, backend):
        """Review is only for rows still lacking a real category."""
        from finance_sync.rows import TRANSACTION_COLUMNS

        client = FakeClient({"category": FakeChoice("personal", 0.35)})
        sync = self._seed_unresolved(backend, client)
        assert len(backend.tab("Finances 2026-10", "Review")[1:]) == 1

        rows = backend.data_rows("Finances 2026-10")
        if rows:
            rows[0][TRANSACTION_COLUMNS.index("category_source")] = "manual"
            rows[0][TRANSACTION_COLUMNS.index("category")] = "pets"
            sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
            assert backend.tab("Finances 2026-10", "Review")[1:] == []


class TestLegacyUnresolvedRows:
    """Rows written before Review routing existed carry source `none` and are
    absent from Review, so nothing caches them and Jev is asked again daily."""

    @pytest.fixture
    def backend(self) -> FakeSheetsBackend:
        from finance_sync.rows import transaction_to_row

        backend = FakeSheetsBackend()
        sync = SheetsSync(backend)
        sid = sync.spreadsheet_for("2026-10")
        legacy = txn().with_category(
            Category.MISCL, CategorySource.NONE
        )
        backend.append_rows(sid, "Transactions", [transaction_to_row(legacy, now=NOW)])
        return backend

    def _factory(self, client):
        def make(overrides, prior=None):
            return LayeredCategorizer(
                overrides=overrides, prior=prior,
                fallback=JevCategorizer("k", 0.8, client=client),
            )
        return make

    def test_it_is_routed_to_review(self, backend):
        client = FakeClient({"category": FakeChoice("personal", 0.35)})
        SheetsSync(backend).sync(
            [txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(backend.tab("Finances 2026-10", "Review")[1:]) == 1

    def test_it_stops_being_reasked(self, backend):
        client = FakeClient({"category": FakeChoice("personal", 0.35)})
        sync = SheetsSync(backend)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        after_first = len(client.calls)
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        sync.sync([txn()], now=NOW, categorizer_factory=self._factory(client))
        assert len(client.calls) == after_first

    def test_a_confident_answer_fills_the_row_instead(self, backend):
        from finance_sync.rows import TRANSACTION_COLUMNS

        client = FakeClient({"category": FakeChoice("pets", 0.95)})
        SheetsSync(backend).sync(
            [txn()], now=NOW, categorizer_factory=self._factory(client))
        row = backend.data_rows("Finances 2026-10")[0]
        assert row[TRANSACTION_COLUMNS.index("category")] == "pets"
        assert backend.tab("Finances 2026-10", "Review")[1:] == []
