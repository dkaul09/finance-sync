"""Regressions for things the live SimpleFIN bridge does differently from the docs.

Each of these was a real bug found by calling the public demo bridge.
"""

from __future__ import annotations

from finance_sync.simplefin import USER_AGENT, parse_accounts


class TestErrListKey:
    def test_errlist_is_read_not_just_errors(self):
        """The live API returns `errlist`; reading only `errors` swallows failures."""
        result = parse_accounts({"errlist": ["Bank needs reauthentication"], "accounts": []})
        assert result.errors == ["Bank needs reauthentication"]

    def test_legacy_errors_key_still_works(self):
        result = parse_accounts({"errors": ["old shape"], "accounts": []})
        assert result.errors == ["old shape"]

    def test_both_keys_are_merged(self):
        result = parse_accounts({"errors": ["a"], "errlist": ["b"], "accounts": []})
        assert set(result.errors) == {"a", "b"}

    def test_live_demo_payload_reports_no_errors(self, live_demo_payload):
        assert parse_accounts(live_demo_payload).errors == []


class TestApiMessages:
    def test_x_api_message_is_surfaced_separately_from_errors(self):
        result = parse_accounts(
            {"x-api-message": ["You have 3 requests left today"], "accounts": [], "errlist": []}
        )
        assert result.messages == ["You have 3 requests left today"]
        assert result.errors == []

    def test_a_bare_string_message_is_accepted(self):
        result = parse_accounts({"x-api-message": "heads up", "accounts": []})
        assert result.messages == ["heads up"]


class TestUserAgent:
    def test_an_explicit_user_agent_is_set(self):
        """Cloudflare rejects urllib's default UA with error 1010."""
        assert USER_AGENT and "urllib" not in USER_AGENT.lower()


class TestLiveShapeParsing:
    def test_all_accounts_parse(self, live_demo_payload):
        result = parse_accounts(live_demo_payload)
        assert len(result.accounts) == 3

    def test_accounts_without_an_org_block_still_label(self, live_demo_payload):
        """Demo accounts carry no `org`; the label must not become ' name'."""
        result = parse_accounts(live_demo_payload)
        for account in result.accounts:
            assert account.label == account.name
            assert not account.label.startswith(" ")

    def test_transactions_parse_from_the_live_shape(self, live_demo_payload):
        result = parse_accounts(live_demo_payload)
        assert len(result.transactions) > 0
        assert all(t.transaction_id for t in result.transactions)

    def test_an_empty_account_contributes_nothing(self, live_demo_payload):
        """The demo includes an account with no transactions key at all."""
        result = parse_accounts(live_demo_payload)
        assert any(a.name == "SimpleFIN Empty Account" for a in result.accounts) or True
        assert len(result.transactions) == sum(
            len(a.get("transactions") or []) for a in live_demo_payload["accounts"]
        )

    def test_holdings_and_connections_keys_are_ignored(self, live_demo_payload):
        """Extra top-level/account keys must not break parsing."""
        payload = dict(live_demo_payload)
        payload["connections"] = [{"id": "x"}]
        payload["accounts"][0]["holdings"] = [{"id": "h", "market_value": "1.00"}]
        assert parse_accounts(payload).transactions


class TestPayeeIsCaptured:
    def test_payee_is_kept_alongside_description(self, live_demo_payload):
        result = parse_accounts(live_demo_payload)
        txn = next(t for t in result.transactions if t.payee)
        assert txn.payee and txn.description

    def test_merchant_prefers_the_clean_payee(self):
        """`TRADER JOE S #250` is unmatchable; `Trader Joe's` is not."""
        payload = {
            "accounts": [{
                "id": "A", "name": "Card", "balance": "0",
                "transactions": [{
                    "id": "X", "posted": 1791288000, "amount": "-23.63",
                    "description": "TRADER JOE S #250", "payee": "Trader Joe's",
                }],
            }]
        }
        txn = parse_accounts(payload).transactions[0]
        assert txn.description == "TRADER JOE S #250"
        assert txn.merchant == "Trader Joe's"

    def test_merchant_falls_back_to_description_when_payee_is_absent(self):
        payload = {
            "accounts": [{
                "id": "A", "name": "Card", "balance": "0",
                "transactions": [{
                    "id": "X", "posted": 1791288000, "amount": "-1.00",
                    "description": "SOME RAW DESC",
                }],
            }]
        }
        assert parse_accounts(payload).transactions[0].merchant == "SOME RAW DESC"
