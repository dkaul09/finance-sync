"""Core data types shared across the sync pipeline."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum


class Category(StrEnum):
    """The fixed category enum, matching the user's budget lines.

    `INCOME` and `TRANSFER` are not budget lines: money in and money moved
    between accounts are not spending, and including them would make the
    budget totals meaningless. They appear on Transactions and are excluded
    from the Budget tab.
    """

    GROCERIES = "groceries"
    LAUNDRY = "laundry"
    PROTEIN_SHAKE = "protein_shake"
    RENT_AND_INSURANCE = "rent_and_insurance"
    MOBILE = "mobile"
    PERSONAL = "personal"
    PETS = "pets"
    UTILITIES = "utilities"
    TRAVEL = "travel"
    HAIRCUT = "haircut"
    GPT = "gpt"
    STARBUCKS_COFFEE = "starbucks_coffee"
    COFFEE_PODS = "coffee_pods"
    OUTSIDE_FOOD = "outside_food"
    MISCL = "miscl"
    SK = "sk"

    # Not budget lines.
    INCOME = "income"
    TRANSFER = "transfer"


#: Budget lines in the order they appear on the Budget tab, with the display
#: name and the default planned amount taken from the user's existing budget.
BUDGET_LINES: list[tuple["Category", str, str]] = [
    (Category.GROCERIES, "Groceries", "152"),
    (Category.LAUNDRY, "Laundry", "20"),
    (Category.PROTEIN_SHAKE, "Protein Shake", "30"),
    (Category.RENT_AND_INSURANCE, "Rent and Insurance", "1355"),
    (Category.MOBILE, "Mobile", "40"),
    (Category.PERSONAL, "Personal", "110"),
    (Category.PETS, "Pets", "0"),
    (Category.UTILITIES, "Utilities", "15"),
    (Category.TRAVEL, "Travel", "0"),
    (Category.HAIRCUT, "Haircut", "29"),
    (Category.GPT, "GPT", "20"),
    (Category.STARBUCKS_COFFEE, "Starbucks Coffee", "15"),
    (Category.COFFEE_PODS, "Coffee Pods", "50"),
    (Category.OUTSIDE_FOOD, "Outside food", "30"),
    (Category.MISCL, "MISCL", "50"),
    (Category.SK, "SK", "100"),
]

#: Categories excluded from budget totals.
NON_BUDGET = {Category.INCOME, Category.TRANSFER}

DISPLAY_NAMES = {category: name for category, name, _ in BUDGET_LINES}


class CategorySource(StrEnum):
    """Where a category came from. `MANUAL` is never overwritten by a sync."""

    OVERRIDE = "override"
    RULES = "rules"
    JEV = "jev"
    MANUAL = "manual"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Transaction:
    """One transaction, in the shape it is written to the Transactions tab."""

    transaction_id: str
    date: dt.date
    account: str
    description: str
    amount: Decimal
    #: SimpleFIN's cleaned merchant name ("Trader Joe's") as opposed to the raw
    #: bank description ("TRADER JOE S #250"). Far better for rule matching.
    #: Not written to the sheet; rules run on freshly fetched transactions.
    payee: str = ""
    posted_date: dt.date | None = None
    category: Category = Category.MISCL
    pending: bool = False
    category_source: CategorySource = CategorySource.NONE
    confidence: float | None = None
    recurring: float | None = None
    business_expense: float | None = None
    last_updated: dt.datetime | None = None

    @property
    def merchant(self) -> str:
        """The best available merchant string for categorization."""
        return self.payee.strip() or self.description.strip()

    @property
    def month_key(self) -> str:
        """`YYYY-MM` of the *transaction* date — the sole routing key.

        Deliberately not the run date, so a run on the 1st files the previous
        month's stragglers into the previous month's spreadsheet.
        """
        return self.date.strftime("%Y-%m")

    def with_category(
        self,
        category: Category,
        source: CategorySource,
        confidence: float | None = None,
        recurring: float | None = None,
        business_expense: float | None = None,
    ) -> Transaction:
        return replace(
            self,
            category=category,
            category_source=source,
            confidence=confidence,
            recurring=recurring,
            business_expense=business_expense,
        )


@dataclass(frozen=True, slots=True)
class Account:
    """A SimpleFIN account. Both cards arrive in one response, one per account."""

    id: str
    name: str
    org: str
    currency: str
    balance: Decimal

    @property
    def label(self) -> str:
        """Human-readable account label written into the `account` column."""
        return f"{self.org} {self.name}".strip() if self.org else self.name


@dataclass(frozen=True, slots=True)
class CategoryResult:
    """A categorization decision for one transaction."""

    transaction_id: str
    category: Category
    source: CategorySource
    confidence: float | None = None
    recurring: float | None = None
    business_expense: float | None = None


@dataclass
class SyncSummary:
    """Per-run counters, logged at the end of every run."""

    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    routed_to_review: int = 0
    requests_made: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def __str__(self) -> str:
        return (
            f"fetched={self.fetched} inserted={self.inserted} "
            f"updated={self.updated} unchanged={self.unchanged} "
            f"routed_to_review={self.routed_to_review} "
            f"requests={self.requests_made} errors={len(self.errors)}"
        )
