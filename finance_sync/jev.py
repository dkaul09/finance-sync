"""Jev categorization: the optional third layer, behind USE_JEV.

Only transactions that overrides and rules could not resolve reach this. Each
one becomes a single System One call carrying three questions evaluated in
parallel, so the recurring and business-expense signals cost almost nothing on
top of the category decision.
"""

from __future__ import annotations

import logging

from .models import Category, CategoryResult, CategorySource, Transaction

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "jev-latest"

#: Each budget line with a one-line definition. Jev picks among these, so the
#: definitions are the whole specification of the decision.
CATEGORY_CRITERIA: dict[str, str] = {
    "groceries": "Supermarkets and grocery stores; food bought to cook or eat at home",
    "outside_food": "Restaurants, cafes serving meals, food delivery, takeout, campus dining",
    "starbucks_coffee": "Coffee, tea or matcha bought from a coffee shop to drink out",
    "coffee_pods": "Coffee, pods, beans or matcha powder bought to make drinks at home",
    "protein_shake": "Protein powder, supplements, vitamins and nutrition products",
    "rent_and_insurance": "Rent, property management payments, and insurance premiums",
    "utilities": "Electricity, gas, water, internet and cable service",
    "mobile": "Mobile phone service and cellular plans",
    "laundry": "Laundromats, wash-and-fold and dry cleaning",
    "haircut": "Haircuts, barbers and salons",
    "pets": "Pet food, pet supplies and veterinary care",
    "travel": "Rideshare, taxis, public transit, fuel, parking, flights and hotels",
    "personal": "General shopping: clothing, household goods, pharmacy, electronics, "
                "and entertainment subscriptions such as streaming or games",
    "gpt": "AI assistant and AI model subscriptions such as ChatGPT, Claude or Perplexity",
    "income": "Money received: salary, payroll deposits, refunds and interest",
    "transfer": "Money moved rather than spent: ATM withdrawals, Venmo, Zelle, "
                "card payments and transfers between accounts",
    "miscl": "Anything that does not clearly fit another category",
}

# `sk` is deliberately absent: it is a manual-only budget line, so Jev is never
# asked to choose it.


def build_state(txn: Transaction) -> dict:
    """The transaction as named JSON fields.

    TypeSafe's guidance is to prefer named fields over prose, so the model sees
    structure rather than a sentence it has to parse.
    """
    return {
        "merchant": txn.merchant,
        "bank_description": txn.description,
        "amount_usd": str(abs(txn.amount)),
        "direction": "money_out" if txn.amount < 0 else "money_in",
        "account": txn.account,
        "date": txn.date.isoformat(),
    }


def build_questions():
    from typesafe_sdk import Choice, Noul

    return {
        "category": Choice(
            instructions="Which budget category does this transaction belong to?",
            criteria=CATEGORY_CRITERIA,
        ),
        "recurring": Noul(
            instructions="Is this a recurring subscription or regular repeating charge?",
        ),
        "business": Noul(
            instructions="Does this look like a business or work expense "
                         "rather than a personal one?",
        ),
    }


class JevCategorizer:
    """Categorizer backed by TypeSafe's Jev model.

    Failures degrade to `miscl`/`none` rather than aborting: a categorization
    outage must not stop the financial data reaching the sheet.
    """

    def __init__(
        self,
        api_key: str,
        confidence_threshold: float = 0.8,
        model: str = DEFAULT_MODEL,
        client=None,
        max_transactions: int = 60,
    ) -> None:
        self._api_key = api_key
        self._threshold = confidence_threshold
        self._model = model
        self._client = client
        self._max = max_transactions

    def _make_client(self):
        if self._client is not None:
            return self._client
        from typesafe_sdk import TypeSafeClient

        return TypeSafeClient(api_key=self._api_key)

    def categorize(self, transactions: list[Transaction]) -> list[CategoryResult]:
        if not transactions:
            return []

        batch = transactions[: self._max]
        if len(transactions) > self._max:
            logger.warning(
                "Jev capped at %s transactions; %s left uncategorized this run",
                self._max,
                len(transactions) - self._max,
            )

        questions = build_questions()
        results: list[CategoryResult] = []

        client = self._make_client()
        close = hasattr(client, "close") and self._client is None
        try:
            for txn in batch:
                results.append(self._ask(client, questions, txn))
        finally:
            if close:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - closing must never mask results
                    pass

        # Anything past the cap is left unresolved rather than silently dropped.
        for txn in transactions[self._max :]:
            results.append(_unresolved(txn))
        return results

    def _ask(self, client, questions, txn: Transaction) -> CategoryResult:
        try:
            response = client.system_one(
                state=build_state(txn), questions=questions, model=self._model
            )
        except Exception as exc:  # noqa: BLE001 - never fail the sync over this
            logger.error("Jev call failed for %s: %s", txn.transaction_id, exc)
            return _unresolved(txn)

        return self._interpret(response, txn)

    def _interpret(self, response, txn: Transaction) -> CategoryResult:
        answers = getattr(response, "answers", {}) or {}
        choice = answers.get("category")
        if choice is None:
            logger.error("Jev returned no category answer for %s", txn.transaction_id)
            return _unresolved(txn)

        raw = getattr(choice, "choice", None)
        try:
            category = Category(str(raw))
        except ValueError:
            logger.error("Jev returned unknown category %r", raw)
            return _unresolved(txn)

        confidence = getattr(choice, "confidence", None)
        confidence = float(confidence) if confidence is not None else 0.0

        recurring = _noul(answers.get("recurring"))
        business = _noul(answers.get("business"))

        # Below the threshold the proposal is not trusted enough to write into
        # Transactions, so it goes to Review for a human decision instead.
        needs_review = confidence < self._threshold

        return CategoryResult(
            transaction_id=txn.transaction_id,
            category=category,
            source=CategorySource.JEV,
            confidence=confidence,
            recurring=recurring,
            business_expense=business,
            needs_review=needs_review,
        )


def _noul(answer) -> float | None:
    """A Noul answer is a bare probability, with no separate confidence."""
    if answer is None:
        return None
    value = getattr(answer, "noul", None)
    return float(value) if value is not None else None


def _unresolved(txn: Transaction) -> CategoryResult:
    return CategoryResult(
        transaction_id=txn.transaction_id,
        category=Category.MISCL,
        source=CategorySource.NONE,
    )


def build_categorizer(config):
    """Return the configured third layer: Jev when enabled, else the null one."""
    from .categorize import NullCategorizer

    if not config.use_jev:
        return NullCategorizer()
    if not config.typesafe_api_key:
        logger.error("USE_JEV is on but TYPESAFE_API_KEY is unset; falling back")
        return NullCategorizer()
    logger.info(
        "Jev enabled (threshold %.2f)", config.jev_confidence_threshold
    )
    return JevCategorizer(
        api_key=config.typesafe_api_key,
        confidence_threshold=config.jev_confidence_threshold,
    )
