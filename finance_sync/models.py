"""Core data types shared across the sync pipeline."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum


class Category(StrEnum):
    """The fixed category enum. Nothing outside this set is ever written."""

    GROCERIES = "groceries"
    RESTAURANTS = "restaurants"
    COFFEE = "coffee"
    TRAVEL = "travel"
    TRANSIT = "transit"
    SHOPPING = "shopping"
    UTILITIES = "utilities"
    SUBSCRIPTIONS = "subscriptions"
    HEALTH = "health"
    ENTERTAINMENT = "entertainment"
    INCOME = "income"
    TRANSFER = "transfer"
    FEES = "fees"
    OTHER = "other"


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
    category: Category = Category.OTHER
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
