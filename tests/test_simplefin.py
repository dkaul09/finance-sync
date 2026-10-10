import datetime as dt
from decimal import Decimal

import pytest

from finance_sync.models import CategorySource
from finance_sync.simplefin import (
    DAILY_REQUEST_LIMIT,
    MAX_WINDOW_DAYS,
    SimpleFinError,
    _split_credentials,
    date_windows,
    parse_accounts,
)


class TestDateWindows:
    def test_short_range_is_one_request(self):
        windows = date_windows(dt.date(2026, 10, 1), dt.date(2026, 10, 9))
        assert windows == [(dt.date(2026, 10, 1), dt.date(2026, 10, 9))]

    def test_ninety_day_backfill_is_exactly_two_requests(self):
        end = dt.date(2026, 10, 9)
        start = end - dt.timedelta(days=89)
        windows = date_windows(start, end)
        assert len(windows) == 2

    def test_no_window_exceeds_the_cap(self):
        windows = date_windows(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        for window_start, window_end in windows:
            assert (window_end - window_start).days + 1 <= MAX_WINDOW_DAYS

    def test_windows_are_contiguous_and_cover_the_range(self):
        start, end = dt.date(2026, 5, 1), dt.date(2026, 9, 30)
        windows = date_windows(start, end)
        assert windows[0][0] == start
        assert windows[-1][1] == end
        for earlier, later in zip(windows, windows[1:]):
            assert later[0] == earlier[1] + dt.timedelta(days=1)

    def test_single_day_range(self):
        day = dt.date(2026, 10, 9)
        assert date_windows(day, day) == [(day, day)]

    def test_reversed_range_is_rejected(self):
        with pytest.raises(ValueError):
            date_windows(dt.date(2026, 10, 9), dt.date(2026, 10, 1))


class TestCredentialHandling:
    def test_userinfo_is_moved_into_an_auth_header(self):
        url, auth = _split_credentials("https://user:secret@bridge.example.com/simplefin")
        assert url == "https://bridge.example.com/simplefin"
        assert auth == "Basic dXNlcjpzZWNyZXQ="

    def test_credentials_are_not_left_in_the_url(self):
        url, _ = _split_credentials("https://user:secret@bridge.example.com/simplefin")
        assert "secret" not in url and "user" not in url

    def test_url_without_credentials_is_untouched(self):
        url, auth = _split_credentials("https://bridge.example.com/simplefin")
        assert url == "https://bridge.example.com/simplefin"
        assert auth is None


class TestParseAccounts:
    def test_both_cards_are_parsed_from_one_response(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        assert {a.id for a in result.accounts} == {"ACT-debit-001", "ACT-credit-002"}

    def test_transactions_from_all_accounts_are_flattened(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        assert len(result.transactions) == 7

    def test_errors_array_is_surfaced_not_swallowed(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        assert len(result.errors) == 1
        assert "attention" in result.errors[0]

    def test_amounts_are_decimal_not_float(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        txn = next(t for t in result.transactions if t.transaction_id == "TXN-1001")
        assert txn.amount == Decimal("-84.32")
        assert isinstance(txn.amount, Decimal)

    def test_pending_flag_is_honoured(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        pending = {t.transaction_id for t in result.transactions if t.pending}
        assert pending == {"TXN-1003", "TXN-2002"}

    def test_pending_transactions_have_no_posted_date(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        txn = next(t for t in result.transactions if t.transaction_id == "TXN-1003")
        assert txn.posted_date is None

    def test_posted_transactions_carry_a_posted_date(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        txn = next(t for t in result.transactions if t.transaction_id == "TXN-2001")
        assert txn.posted_date == dt.date(2026, 10, 8)

    def test_date_comes_from_transacted_at_not_posted(self, accounts_payload):
        """Routing uses the transaction date, so transacted_at wins."""
        result = parse_accounts(accounts_payload)
        txn = next(t for t in result.transactions if t.transaction_id == "TXN-1001")
        assert txn.date == dt.date(2026, 10, 5)
        assert txn.posted_date == dt.date(2026, 10, 6)

    def test_account_label_identifies_the_card(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        txn = next(t for t in result.transactions if t.transaction_id == "TXN-2001")
        assert txn.account == "My Card Rewards Visa"

    def test_everything_starts_uncategorized(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        assert all(t.category_source == CategorySource.NONE for t in result.transactions)

    def test_month_key_uses_transaction_date(self, accounts_payload):
        result = parse_accounts(accounts_payload)
        txn = next(t for t in result.transactions if t.transaction_id == "TXN-2003")
        assert txn.date == dt.date(2026, 9, 8)
        assert txn.month_key == "2026-09"

    def test_empty_payload_is_not_an_error(self):
        result = parse_accounts({"errors": [], "accounts": []})
        assert result.transactions == [] and result.errors == []

    def test_missing_keys_are_tolerated(self):
        result = parse_accounts({})
        assert result.accounts == [] and result.transactions == []

    def test_transaction_without_id_is_rejected(self):
        payload = {
            "accounts": [
                {"id": "A", "name": "N", "balance": "0", "transactions": [{"posted": 1, "amount": "1"}]}
            ]
        }
        with pytest.raises(SimpleFinError, match="without an id"):
            parse_accounts(payload)

    def test_transaction_without_any_date_is_rejected(self):
        payload = {
            "accounts": [
                {"id": "A", "name": "N", "balance": "0",
                 "transactions": [{"id": "X", "posted": 0, "amount": "1"}]}
            ]
        }
        with pytest.raises(SimpleFinError, match="neither transacted_at nor posted"):
            parse_accounts(payload)

    def test_unparseable_amount_is_rejected(self):
        payload = {
            "accounts": [
                {"id": "A", "name": "N", "balance": "0",
                 "transactions": [{"id": "X", "posted": 1759190400, "amount": "not-money"}]}
            ]
        }
        with pytest.raises(SimpleFinError, match="bad amount"):
            parse_accounts(payload)

    def test_non_object_payload_is_rejected(self):
        with pytest.raises(SimpleFinError, match="expected a JSON object"):
            parse_accounts([])  # type: ignore[arg-type]


class TestSettledPayload:
    def test_settling_clears_pending_and_sets_posted_date(self, settled_payload):
        result = parse_accounts(settled_payload)
        txn = result.transactions[0]
        assert txn.transaction_id == "TXN-1003"
        assert txn.pending is False
        assert txn.posted_date == dt.date(2026, 10, 13)

    def test_settled_amount_differs_from_pending_amount(self, settled_payload):
        """The settle path must be able to change the amount."""
        txn = parse_accounts(settled_payload).transactions[0]
        assert txn.amount == Decimal("-21.25")

    def test_id_is_stable_across_settlement(self, accounts_payload, settled_payload):
        pending = next(
            t for t in parse_accounts(accounts_payload).transactions
            if t.transaction_id == "TXN-1003"
        )
        settled = parse_accounts(settled_payload).transactions[0]
        assert pending.transaction_id == settled.transaction_id
        assert pending.date == settled.date


def test_daily_limit_constant_matches_the_documented_budget():
    assert DAILY_REQUEST_LIMIT == 24
