"""Layered categorization: overrides, then rules, then a pluggable categorizer.

Cheapest first, first match wins. The expensive layer (Jev) only ever sees
transactions the free layers could not resolve.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol, runtime_checkable

from .models import Category, CategoryResult, CategorySource, Transaction

logger = logging.getLogger(__name__)


def normalize(text: str) -> str:
    """Fold a merchant string for matching.

    Bank descriptions mangle punctuation: SimpleFIN's payee is `Trader Joe's`
    while the description is `TRADER JOE S #250`. Stripping non-alphanumerics
    to single spaces makes both normalize to `TRADER JOE S`, so one pattern
    matches either field.
    """
    return re.sub(r"[^A-Z0-9]+", " ", (text or "").upper()).strip()


#: Seed merchant -> category rules, keyed by a normalized substring.
#: Built from real transaction data rather than guesses. Order does not matter
#: here: matching always prefers the longest pattern, so `UBER EATS` wins over
#: `UBER` and the two land in different categories.
SEED_RULES: dict[str, Category] = {
    # groceries
    "TRADER JOE": Category.GROCERIES,
    "WHOLE FOODS": Category.GROCERIES,
    "SPROUTS": Category.GROCERIES,
    "SAFEWAY": Category.GROCERIES,
    "RALPHS": Category.GROCERIES,
    "VONS": Category.GROCERIES,
    "KROGER": Category.GROCERIES,
    "ALDI": Category.GROCERIES,
    "COSTCO": Category.GROCERIES,
    "SUPERETTE": Category.GROCERIES,
    # coffee
    "STARBUCKS": Category.COFFEE,
    "BLUE BOTTLE": Category.COFFEE,
    "PEETS": Category.COFFEE,
    "DUNKIN": Category.COFFEE,
    "PHILZ": Category.COFFEE,
    "DRIP DADDY": Category.COFFEE,
    "JADE LEAF MATCHA": Category.COFFEE,
    "NESPRESSO": Category.COFFEE,
    "DULCE": Category.COFFEE,
    # restaurants
    "UBER EATS": Category.RESTAURANTS,
    "DOORDASH": Category.RESTAURANTS,
    "GRUBHUB": Category.RESTAURANTS,
    "POSTMATES": Category.RESTAURANTS,
    "CHIPOTLE": Category.RESTAURANTS,
    "SWEETGREEN": Category.RESTAURANTS,
    "USC HOSPITALITY": Category.RESTAURANTS,
    "GIARDINO": Category.RESTAURANTS,
    # transit
    "UBER": Category.TRANSIT,
    "LYFT": Category.TRANSIT,
    "MTA": Category.TRANSIT,
    "METRO": Category.TRANSIT,
    "BART": Category.TRANSIT,
    "CALTRAIN": Category.TRANSIT,
    "SHELL": Category.TRANSIT,
    "CHEVRON": Category.TRANSIT,
    "PARKING": Category.TRANSIT,
    # travel
    "UNITED AIRLINES": Category.TRAVEL,
    "DELTA AIR": Category.TRAVEL,
    "SOUTHWEST AIR": Category.TRAVEL,
    "AMERICAN AIR": Category.TRAVEL,
    "AIRBNB": Category.TRAVEL,
    "MARRIOTT": Category.TRAVEL,
    "HILTON": Category.TRAVEL,
    "EXPEDIA": Category.TRAVEL,
    # shopping
    "TARGET": Category.SHOPPING,
    "AMAZON": Category.SHOPPING,
    "WALMART": Category.SHOPPING,
    "BEST BUY": Category.SHOPPING,
    "UNDER ARMOUR": Category.SHOPPING,
    "ZARA": Category.SHOPPING,
    "UNIQLO": Category.SHOPPING,
    "IKEA": Category.SHOPPING,
    "APPLE STORE": Category.SHOPPING,
    # subscriptions
    "NETFLIX": Category.SUBSCRIPTIONS,
    "SPOTIFY": Category.SUBSCRIPTIONS,
    "HULU": Category.SUBSCRIPTIONS,
    "DISNEY PLUS": Category.SUBSCRIPTIONS,
    "YOUTUBE PREMIUM": Category.SUBSCRIPTIONS,
    "ICLOUD": Category.SUBSCRIPTIONS,
    "ANTHROPIC": Category.SUBSCRIPTIONS,
    "OPENAI": Category.SUBSCRIPTIONS,
    "VERCEL": Category.SUBSCRIPTIONS,
    "GITHUB": Category.SUBSCRIPTIONS,
    "PADDLE": Category.SUBSCRIPTIONS,
    "LINK COM": Category.SUBSCRIPTIONS,
    # entertainment
    "PLAYSTATION": Category.ENTERTAINMENT,
    "STEAM GAMES": Category.ENTERTAINMENT,
    "XBOX": Category.ENTERTAINMENT,
    "AMC": Category.ENTERTAINMENT,
    "TICKETMASTER": Category.ENTERTAINMENT,
    "GEOGUESSR": Category.ENTERTAINMENT,
    # utilities
    "COMCAST": Category.UTILITIES,
    "XFINITY": Category.UTILITIES,
    "VERIZON": Category.UTILITIES,
    "AT T": Category.UTILITIES,
    "T MOBILE": Category.UTILITIES,
    "SPECTRUM": Category.UTILITIES,
    "SOCALGAS": Category.UTILITIES,
    "DWP": Category.UTILITIES,
    "PG E": Category.UTILITIES,
    # rent
    "TRIPALINK": Category.RENT,
    "APPFOLIO": Category.RENT,
    "GREYSTAR": Category.RENT,
    # health
    "CVS": Category.HEALTH,
    "WALGREENS": Category.HEALTH,
    "RITE AID": Category.HEALTH,
    "ONE MEDICAL": Category.HEALTH,
    "MSI INSURANCE": Category.HEALTH,
    # fees
    "ATM FEE": Category.FEES,
    "OVERDRAFT": Category.FEES,
    "SERVICE CHARGE": Category.FEES,
    "FOREIGN TRANSACTION FEE": Category.FEES,
    "LATE FEE": Category.FEES,
    # income
    "PAYROLL": Category.INCOME,
    "DIRECT DEPOSIT": Category.INCOME,
    "PAY DAY": Category.INCOME,
    "INTEREST PAID": Category.INCOME,
    # transfer
    "VENMO": Category.TRANSFER,
    "ZELLE": Category.TRANSFER,
    "CASH APP": Category.TRANSFER,
    "WITHDRAWAL": Category.TRANSFER,
    "PAYMENT THANK YOU": Category.TRANSFER,
    "AUTOPAY": Category.TRANSFER,
    "ONLINE TRANSFER": Category.TRANSFER,
}


class Override:
    """One row of the Overrides tab: a pattern mapped to a category."""

    __slots__ = ("pattern", "category", "is_regex", "_compiled", "_normalized")

    def __init__(self, pattern: str, category: Category, is_regex: bool = False) -> None:
        self.pattern = pattern
        self.category = category
        self.is_regex = is_regex
        self._compiled: re.Pattern[str] | None = None
        self._normalized = normalize(pattern)
        if is_regex:
            try:
                self._compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                # A bad hand-written regex must not break the whole run.
                logger.error("Ignoring invalid regex override %r: %s", pattern, exc)

    def matches(self, txn: Transaction) -> bool:
        if self.is_regex:
            if self._compiled is None:
                return False
            return bool(
                self._compiled.search(txn.description) or self._compiled.search(txn.payee)
            )
        if not self._normalized:
            return False
        return self._normalized in normalize(txn.merchant) or self._normalized in normalize(
            txn.description
        )


def parse_overrides(rows: list[list[str]]) -> list[Override]:
    """Parse Overrides-tab rows. Unparseable rows are logged and skipped."""
    overrides: list[Override] = []
    for row in rows:
        padded = list(row) + ["", "", ""]
        pattern, raw_category, raw_regex = padded[0], padded[1], padded[2]
        pattern = (pattern or "").strip()
        if not pattern or pattern.lower() == "pattern":
            continue
        try:
            category = Category((raw_category or "").strip().lower())
        except ValueError:
            logger.error(
                "Override %r has unknown category %r; skipping", pattern, raw_category
            )
            continue
        is_regex = (raw_regex or "").strip().upper() in {"TRUE", "YES", "1"}
        overrides.append(Override(pattern, category, is_regex))
    return overrides


@runtime_checkable
class Categorizer(Protocol):
    """The pluggable third layer. Jev implements this; the default does nothing."""

    def categorize(self, transactions: list[Transaction]) -> list[CategoryResult]: ...


class NullCategorizer(Categorizer):
    """Default third layer: resolves nothing, so the system works with Jev off."""

    def categorize(self, transactions: list[Transaction]) -> list[CategoryResult]:
        return [
            CategoryResult(
                transaction_id=txn.transaction_id,
                category=Category.OTHER,
                source=CategorySource.NONE,
            )
            for txn in transactions
        ]


def match_rules(txn: Transaction, rules: dict[str, Category] | None = None) -> Category | None:
    """Match against the seed rules, preferring the most specific pattern.

    Longest-first is what keeps `UBER EATS` (restaurants) from being captured
    by `UBER` (transit).
    """
    rules = SEED_RULES if rules is None else rules
    merchant = normalize(txn.merchant)
    description = normalize(txn.description)

    best: tuple[int, Category] | None = None
    for pattern, category in rules.items():
        if pattern in merchant or pattern in description:
            if best is None or len(pattern) > best[0]:
                best = (len(pattern), category)
    return best[1] if best is not None else None


class LayeredCategorizer:
    """Overrides -> seed rules -> pluggable categorizer."""

    def __init__(
        self,
        overrides: list[Override] | None = None,
        rules: dict[str, Category] | None = None,
        fallback: Categorizer | None = None,
    ) -> None:
        self._overrides = overrides or []
        self._rules = SEED_RULES if rules is None else rules
        self._fallback = fallback or NullCategorizer()

    def categorize(self, transactions: list[Transaction]) -> list[CategoryResult]:
        results: dict[str, CategoryResult] = {}
        unresolved: list[Transaction] = []

        for txn in transactions:
            override = next((o for o in self._overrides if o.matches(txn)), None)
            if override is not None:
                results[txn.transaction_id] = CategoryResult(
                    txn.transaction_id, override.category, CategorySource.OVERRIDE
                )
                continue

            rule_category = match_rules(txn, self._rules)
            if rule_category is not None:
                results[txn.transaction_id] = CategoryResult(
                    txn.transaction_id, rule_category, CategorySource.RULES
                )
                continue

            unresolved.append(txn)

        if unresolved:
            logger.info(
                "%s of %s transactions unresolved by overrides and rules",
                len(unresolved),
                len(transactions),
            )
            for result in self._fallback.categorize(unresolved):
                results[result.transaction_id] = result

        # Preserve input order.
        return [results[t.transaction_id] for t in transactions]


def apply_results(
    transactions: list[Transaction], results: list[CategoryResult]
) -> list[Transaction]:
    """Fold categorization decisions back onto transactions."""
    by_id = {r.transaction_id: r for r in results}
    out: list[Transaction] = []
    for txn in transactions:
        result = by_id.get(txn.transaction_id)
        if result is None:
            out.append(txn)
            continue
        out.append(
            txn.with_category(
                result.category,
                result.source,
                confidence=result.confidence,
                recurring=result.recurring,
                business_expense=result.business_expense,
            )
        )
    return out
