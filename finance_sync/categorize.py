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
    "SMART FINAL": Category.GROCERIES,
    # outside food (eating out and delivery)
    "UBER EATS": Category.OUTSIDE_FOOD,
    "DOORDASH": Category.OUTSIDE_FOOD,
    "GRUBHUB": Category.OUTSIDE_FOOD,
    "POSTMATES": Category.OUTSIDE_FOOD,
    "CAVIAR": Category.OUTSIDE_FOOD,
    "CHIPOTLE": Category.OUTSIDE_FOOD,
    "SWEETGREEN": Category.OUTSIDE_FOOD,
    "USC HOSPITALITY": Category.OUTSIDE_FOOD,
    "GIARDINO": Category.OUTSIDE_FOOD,
    "DULCE": Category.OUTSIDE_FOOD,
    "TAMIX": Category.OUTSIDE_FOOD,
    "YBSY": Category.OUTSIDE_FOOD,
    "PANDA EXPRESS": Category.OUTSIDE_FOOD,
    "RAISING CANE": Category.OUTSIDE_FOOD,
    # coffee bought out
    "STARBUCKS": Category.STARBUCKS_COFFEE,
    "BLUE BOTTLE": Category.STARBUCKS_COFFEE,
    "PEETS": Category.STARBUCKS_COFFEE,
    "DUNKIN": Category.STARBUCKS_COFFEE,
    "PHILZ": Category.STARBUCKS_COFFEE,
    "DRIP DADDY": Category.STARBUCKS_COFFEE,
    "COFFEE BEAN": Category.STARBUCKS_COFFEE,
    # coffee consumed at home
    "NESPRESSO": Category.COFFEE_PODS,
    "JADE LEAF MATCHA": Category.COFFEE_PODS,
    "KEURIG": Category.COFFEE_PODS,
    "LAVAZZA": Category.COFFEE_PODS,
    # travel (includes rideshare: there is no separate transit line)
    "UBER": Category.TRAVEL,
    "LYFT": Category.TRAVEL,
    "MTA": Category.TRAVEL,
    "METRO": Category.TRAVEL,
    "BART": Category.TRAVEL,
    "CALTRAIN": Category.TRAVEL,
    "AMTRAK": Category.TRAVEL,
    "UNITED AIRLINES": Category.TRAVEL,
    "DELTA AIR": Category.TRAVEL,
    "SOUTHWEST AIR": Category.TRAVEL,
    "AMERICAN AIR": Category.TRAVEL,
    "ALASKA AIR": Category.TRAVEL,
    "AIRBNB": Category.TRAVEL,
    "MARRIOTT": Category.TRAVEL,
    "HILTON": Category.TRAVEL,
    "EXPEDIA": Category.TRAVEL,
    "SHELL": Category.TRAVEL,
    "CHEVRON": Category.TRAVEL,
    "PARKING": Category.TRAVEL,
    # rent and insurance
    "TRIPALINK": Category.RENT_AND_INSURANCE,
    "APPFOLIO": Category.RENT_AND_INSURANCE,
    "GREYSTAR": Category.RENT_AND_INSURANCE,
    "MSI INSURANCE": Category.RENT_AND_INSURANCE,
    "LEMONADE INS": Category.RENT_AND_INSURANCE,
    "GEICO": Category.RENT_AND_INSURANCE,
    "STATE FARM": Category.RENT_AND_INSURANCE,
    # mobile
    "VERIZON": Category.MOBILE,
    "T MOBILE": Category.MOBILE,
    "AT T": Category.MOBILE,
    "MINT MOBILE": Category.MOBILE,
    "GOOGLE FI": Category.MOBILE,
    # utilities
    "COMCAST": Category.UTILITIES,
    "XFINITY": Category.UTILITIES,
    "SPECTRUM": Category.UTILITIES,
    "SOCALGAS": Category.UTILITIES,
    "DWP": Category.UTILITIES,
    "PG E": Category.UTILITIES,
    "CONED": Category.UTILITIES,
    # personal (shopping, household, entertainment subscriptions)
    "TARGET": Category.PERSONAL,
    "AMAZON": Category.PERSONAL,
    "WALMART": Category.PERSONAL,
    "BEST BUY": Category.PERSONAL,
    "UNDER ARMOUR": Category.PERSONAL,
    "ZARA": Category.PERSONAL,
    "UNIQLO": Category.PERSONAL,
    "IKEA": Category.PERSONAL,
    "APPLE STORE": Category.PERSONAL,
    "CVS": Category.PERSONAL,
    "WALGREENS": Category.PERSONAL,
    "RITE AID": Category.PERSONAL,
    "NETFLIX": Category.PERSONAL,
    "SPOTIFY": Category.PERSONAL,
    "HULU": Category.PERSONAL,
    "DISNEY PLUS": Category.PERSONAL,
    "YOUTUBE PREMIUM": Category.PERSONAL,
    "PLAYSTATION": Category.PERSONAL,
    "STEAM GAMES": Category.PERSONAL,
    "XBOX": Category.PERSONAL,
    "AMC": Category.PERSONAL,
    "TICKETMASTER": Category.PERSONAL,
    "GEOGUESSR": Category.PERSONAL,
    # AI subscriptions
    "ANTHROPIC": Category.GPT,
    "OPENAI": Category.GPT,
    "PERPLEXIT": Category.GPT,
    "CHATGPT": Category.GPT,
    "CURSOR": Category.GPT,
    # haircut
    "SUPERCUTS": Category.HAIRCUT,
    "BARBER": Category.HAIRCUT,
    "GREAT CLIPS": Category.HAIRCUT,
    # laundry
    "LAUNDRY": Category.LAUNDRY,
    "LAUNDROMAT": Category.LAUNDRY,
    "WASH AND FOLD": Category.LAUNDRY,
    # protein / supplements
    "PROTEIN": Category.PROTEIN_SHAKE,
    "MYPROTEIN": Category.PROTEIN_SHAKE,
    "GNC": Category.PROTEIN_SHAKE,
    "BODYBUILDING COM": Category.PROTEIN_SHAKE,
    "OPTIMUM NUTRITION": Category.PROTEIN_SHAKE,
    # pets
    "PETCO": Category.PETS,
    "PETSMART": Category.PETS,
    "CHEWY": Category.PETS,
    # income
    "PAYROLL": Category.INCOME,
    "DIRECT DEPOSIT": Category.INCOME,
    "PAY DAY": Category.INCOME,
    "INTEREST PAID": Category.INCOME,
    "REFUND": Category.INCOME,
    # transfers (money moved, not spent)
    "VENMO": Category.TRANSFER,
    "ZELLE": Category.TRANSFER,
    "CASH APP": Category.TRANSFER,
    "WITHDRAWAL": Category.TRANSFER,
    "PAYMENT THANK YOU": Category.TRANSFER,
    "AUTOPAY": Category.TRANSFER,
    "ONLINE TRANSFER": Category.TRANSFER,
    "ATM": Category.TRANSFER,
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
                category=Category.MISCL,
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
