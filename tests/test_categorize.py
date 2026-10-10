"""Categorization layering: overrides beat rules, rules beat the fallback."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from finance_sync.categorize import (
    SEED_RULES,
    LayeredCategorizer,
    NullCategorizer,
    Override,
    apply_results,
    match_rules,
    normalize,
    parse_overrides,
)
from finance_sync.models import Category, CategoryResult, CategorySource, Transaction


def txn(payee: str, description: str = "", txn_id: str = "T1") -> Transaction:
    return Transaction(
        transaction_id=txn_id,
        date=dt.date(2026, 10, 5),
        account="Chase Freedom Unlimited (5047)",
        description=description or payee,
        payee=payee,
        amount=Decimal("-10.00"),
    )


class TestNormalize:
    def test_punctuation_is_folded_away(self):
        """The whole point: payee and description must normalize alike."""
        assert normalize("Trader Joe's") == normalize("TRADER JOE S")

    def test_store_numbers_separate_cleanly(self):
        assert normalize("TRADER JOE S #250") == "TRADER JOE S 250"

    def test_asterisks_and_runs_of_space_collapse(self):
        assert normalize("UBER   *EATS") == "UBER EATS"

    def test_empty_input(self):
        assert normalize("") == ""
        assert normalize(None) == ""  # type: ignore[arg-type]


class TestSeedRules:
    @pytest.mark.parametrize(
        "payee,description,expected",
        [
            ("Trader Joe's", "TRADER JOE S #250", Category.GROCERIES),
            ("Target", "TARGET STORE T3217", Category.SHOPPING),
            ("Starbucks", "STARBUCKS STORE 25043", Category.COFFEE),
            ("Uber Eats", "UBER   *EATS", Category.RESTAURANTS),
            ("Uber", "UBER   *TRIP", Category.TRANSIT),
            ("Lyft", "LYFT *SHARED 09-20", Category.TRANSIT),
            ("Hulu", "HULU 877-8244", Category.SUBSCRIPTIONS),
            ("PlayStation", "VENMO *PLAYSTATION", Category.ENTERTAINMENT),
            ("Nespresso", "NESPRESSO USA, INC.", Category.COFFEE),
            ("Under Armour", "UNDER ARMOUR 1234", Category.SHOPPING),
            ("Tripalink a L E Web", "Tripalink-a1l2E0 WEB PMTS", Category.RENT),
            ("AppFolio", "AppFolio, Inc. F WEB PMTS", Category.RENT),
            ("Anthropic", "ANTHROPIC CLAUDE", Category.SUBSCRIPTIONS),
            ("Supercuts", "Supercuts USC", None),
        ],
    )
    def test_real_merchants_from_live_data(self, payee, description, expected):
        assert match_rules(txn(payee, description)) == expected

    def test_longest_pattern_wins(self):
        """UBER EATS must not be swallowed by the shorter UBER rule."""
        assert match_rules(txn("Uber Eats", "UBER *EATS")) == Category.RESTAURANTS
        assert match_rules(txn("Uber", "UBER *TRIP")) == Category.TRANSIT

    def test_apostrophe_mismatch_still_matches(self):
        """The bug real data exposed: description drops the apostrophe."""
        assert match_rules(txn("", "TRADER JOE S #250")) == Category.GROCERIES

    def test_matching_is_case_insensitive(self):
        assert match_rules(txn("trader joe's")) == Category.GROCERIES

    def test_unknown_merchant_is_unresolved(self):
        assert match_rules(txn("Totally Unknown LLC")) is None

    def test_description_is_used_when_payee_is_blank(self):
        assert match_rules(txn("", "NETFLIX.COM")) == Category.SUBSCRIPTIONS

    def test_every_seed_category_is_a_real_category(self):
        assert all(isinstance(c, Category) for c in SEED_RULES.values())

    def test_seed_patterns_are_already_normalized(self):
        """A pattern that is not normalized could never match."""
        for pattern in SEED_RULES:
            assert normalize(pattern) == pattern, pattern


class TestOverrides:
    def test_substring_override_wins_over_a_rule(self):
        """Target is shopping by rule; an override must beat it."""
        cat = LayeredCategorizer(overrides=[Override("TARGET", Category.GROCERIES)])
        result = cat.categorize([txn("Target", "TARGET STORE T3217")])[0]
        assert result.category == Category.GROCERIES
        assert result.source == CategorySource.OVERRIDE

    def test_override_matches_the_raw_description_too(self):
        cat = LayeredCategorizer(overrides=[Override("T3217", Category.HEALTH)])
        assert cat.categorize([txn("Target", "TARGET STORE T3217")])[0].category == Category.HEALTH

    def test_regex_override(self):
        cat = LayeredCategorizer(
            overrides=[Override(r"USC\s+HOSPITALITY", Category.RESTAURANTS, is_regex=True)]
        )
        result = cat.categorize([txn("Usc Hospitality Retail", "USC HOSPITALITY RETAIL")])[0]
        assert result.category == Category.RESTAURANTS
        assert result.source == CategorySource.OVERRIDE

    def test_an_invalid_regex_is_skipped_not_fatal(self):
        """A bad hand-written regex must not break the nightly run."""
        cat = LayeredCategorizer(overrides=[Override("(unclosed", Category.FEES, is_regex=True)])
        result = cat.categorize([txn("Trader Joe's", "TRADER JOE S #250")])[0]
        assert result.category == Category.GROCERIES

    def test_first_matching_override_wins(self):
        cat = LayeredCategorizer(
            overrides=[
                Override("TARGET", Category.GROCERIES),
                Override("TARGET", Category.HEALTH),
            ]
        )
        assert cat.categorize([txn("Target")])[0].category == Category.GROCERIES

    def test_apostrophe_insensitive_override(self):
        cat = LayeredCategorizer(overrides=[Override("Trader Joe's", Category.RESTAURANTS)])
        assert cat.categorize([txn("", "TRADER JOE S #250")])[0].category == Category.RESTAURANTS


class TestParseOverrides:
    def test_header_row_is_skipped(self):
        assert parse_overrides([["pattern", "category", "is_regex"]]) == []

    def test_blank_rows_are_skipped(self):
        assert parse_overrides([[], ["", "groceries"]]) == []

    def test_regex_flag_is_read(self):
        overrides = parse_overrides([["^UBER", "transit", "TRUE"]])
        assert len(overrides) == 1 and overrides[0].is_regex

    def test_missing_regex_column_defaults_to_substring(self):
        overrides = parse_overrides([["UBER", "transit"]])
        assert len(overrides) == 1 and not overrides[0].is_regex

    def test_unknown_category_is_skipped_not_fatal(self):
        assert parse_overrides([["UBER", "brunch"]]) == []

    def test_valid_rows_survive_an_invalid_neighbour(self):
        overrides = parse_overrides([["UBER", "brunch"], ["LYFT", "transit"]])
        assert [o.pattern for o in overrides] == ["LYFT"]


class TestLayering:
    def test_rules_apply_when_no_override_matches(self):
        result = LayeredCategorizer().categorize([txn("Starbucks")])[0]
        assert result.category == Category.COFFEE
        assert result.source == CategorySource.RULES

    def test_unresolved_falls_through_to_the_null_categorizer(self):
        result = LayeredCategorizer().categorize([txn("Mystery Merchant")])[0]
        assert result.category == Category.OTHER
        assert result.source == CategorySource.NONE

    def test_the_fallback_only_sees_unresolved_transactions(self):
        seen: list[str] = []

        class Spy(NullCategorizer):
            def categorize(self, transactions):
                seen.extend(t.payee for t in transactions)
                return super().categorize(transactions)

        cat = LayeredCategorizer(fallback=Spy())
        cat.categorize([txn("Starbucks", txn_id="A"), txn("Mystery Co", txn_id="B")])
        assert seen == ["Mystery Co"]

    def test_the_fallback_is_not_called_at_all_when_everything_resolves(self):
        calls = []

        class Spy(NullCategorizer):
            def categorize(self, transactions):
                calls.append(len(transactions))
                return super().categorize(transactions)

        LayeredCategorizer(fallback=Spy()).categorize([txn("Starbucks")])
        assert calls == []

    def test_input_order_is_preserved(self):
        batch = [txn("Starbucks", txn_id="A"), txn("Mystery", txn_id="B"), txn("Target", txn_id="C")]
        results = LayeredCategorizer().categorize(batch)
        assert [r.transaction_id for r in results] == ["A", "B", "C"]

    def test_null_categorizer_satisfies_the_protocol(self):
        """The whole point of the interface: Jev-off must be a working default."""
        from finance_sync.categorize import Categorizer

        assert isinstance(NullCategorizer(), Categorizer)


class TestApplyResults:
    def test_categories_are_folded_onto_transactions(self):
        batch = [txn("Starbucks")]
        results = LayeredCategorizer().categorize(batch)
        updated = apply_results(batch, results)
        assert updated[0].category == Category.COFFEE
        assert updated[0].category_source == CategorySource.RULES

    def test_financial_fields_are_untouched(self):
        batch = [txn("Starbucks")]
        updated = apply_results(batch, LayeredCategorizer().categorize(batch))
        assert updated[0].amount == batch[0].amount
        assert updated[0].transaction_id == batch[0].transaction_id

    def test_jev_extras_are_carried_through(self):
        batch = [txn("Mystery")]
        results = [
            CategoryResult("T1", Category.SUBSCRIPTIONS, CategorySource.JEV,
                           confidence=0.91, recurring=0.97, business_expense=0.12)
        ]
        updated = apply_results(batch, results)
        assert updated[0].confidence == 0.91
        assert updated[0].recurring == 0.97
        assert updated[0].business_expense == 0.12

    def test_a_transaction_with_no_result_is_left_alone(self):
        batch = [txn("Starbucks")]
        assert apply_results(batch, [])[0] == batch[0]
