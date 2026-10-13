"""SimpleFIN client: fetch accounts + transactions, respecting API limits."""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import Account, Transaction

logger = logging.getLogger(__name__)

#: SimpleFIN caps a single request's date range at 60 days.
MAX_WINDOW_DAYS = 60

#: The published rate limit. A 90-day backfill costs 2 of these.
DAILY_REQUEST_LIMIT = 24

#: The bridge sits behind Cloudflare, which rejects urllib's default
#: `Python-urllib/x.y` User-Agent with error 1010. An explicit one is required.
USER_AGENT = "finance-sync/0.1 (+https://github.com/)"


class SimpleFinError(RuntimeError):
    """Raised when SimpleFIN returns an unusable response."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    accounts: list[Account]
    transactions: list[Transaction]
    errors: list[str]
    requests_made: int
    #: `x-api-message` notices: informational, not failures.
    messages: tuple[str, ...] | list[str] = ()


def date_windows(
    start: dt.date, end: dt.date, max_days: int = MAX_WINDOW_DAYS
) -> list[tuple[dt.date, dt.date]]:
    """Split an inclusive date range into windows of at most `max_days`.

    A 90-day range yields exactly two windows, so the backfill stays inside the
    per-request cap without any guesswork at the call site.
    """
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    windows: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + dt.timedelta(days=max_days - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + dt.timedelta(days=1)
    return windows


def _split_credentials(access_url: str) -> tuple[str, str | None]:
    """Pull basic-auth credentials out of the access URL's userinfo section.

    Returns the credential-free URL and an `Authorization` header value. Keeping
    the credentials out of the request URL keeps them out of error messages.
    """
    parsed = urllib.parse.urlsplit(access_url)
    if not parsed.username:
        return access_url, None

    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    clean = urllib.parse.urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )
    raw = f"{urllib.parse.unquote(parsed.username)}:{urllib.parse.unquote(parsed.password or '')}"
    token = base64.b64encode(raw.encode()).decode()
    return clean, f"Basic {token}"


def _to_decimal(value: object, field: str, txn_id: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise SimpleFinError(
            f"transaction {txn_id}: bad {field} {value!r}"
        ) from exc


def _epoch_to_date(value: object) -> dt.date | None:
    """SimpleFIN uses epoch seconds; 0 or null means 'not posted yet'."""
    if not value:
        return None
    return dt.datetime.fromtimestamp(int(value), tz=dt.UTC).date()


def parse_accounts(payload: dict) -> FetchResult:
    """Turn one `/accounts` response into accounts, transactions and errors.

    The `errors` array is surfaced, never swallowed — callers log it loudly and
    exit non-zero so a scheduled run's failure is visible.
    """
    if not isinstance(payload, dict):
        raise SimpleFinError(f"expected a JSON object, got {type(payload).__name__}")

    # The live bridge returns `errlist`; older docs say `errors`. Reading only
    # one of them would silently swallow exactly what must never be swallowed.
    errors = [
        str(e)
        for key in ("errors", "errlist")
        for e in (payload.get(key) or [])
    ]

    # Out-of-band notices (rate-limit warnings, deprecations) arrive here.
    api_message = payload.get("x-api-message") or []
    if isinstance(api_message, str):
        api_message = [api_message]
    messages = [str(m) for m in api_message]
    accounts: list[Account] = []
    transactions: list[Transaction] = []

    for raw_account in payload.get("accounts") or []:
        org = raw_account.get("org") or {}
        account = Account(
            id=str(raw_account.get("id", "")),
            name=str(raw_account.get("name", "")),
            org=str(org.get("name") or org.get("domain") or ""),
            currency=str(raw_account.get("currency", "USD")),
            balance=_to_decimal(
                raw_account.get("balance", "0"), "balance", raw_account.get("id", "?")
            ),
        )
        accounts.append(account)

        for raw_txn in raw_account.get("transactions") or []:
            transactions.append(_parse_transaction(raw_txn, account))

    return FetchResult(
        accounts=accounts,
        transactions=transactions,
        errors=errors,
        requests_made=0,
        messages=messages,
    )


def _parse_transaction(raw: dict, account: Account) -> Transaction:
    txn_id = str(raw.get("id") or "")
    if not txn_id:
        raise SimpleFinError(f"transaction without an id in account {account.id}")

    posted_date = _epoch_to_date(raw.get("posted"))
    transacted_date = _epoch_to_date(raw.get("transacted_at"))

    # A pending transaction may carry posted == 0, so fall back in both
    # directions and only then give up.
    date = transacted_date or posted_date
    if date is None:
        raise SimpleFinError(f"transaction {txn_id} has neither transacted_at nor posted")

    # SimpleFIN marks pending either with an explicit flag or by leaving
    # `posted` unset. Treat both as pending.
    pending = bool(raw.get("pending", False)) or posted_date is None

    # Keep both: `description` matches the bank statement the user eyeballs,
    # while `payee` is SimpleFIN's normalized merchant name used for matching.
    description = str(raw.get("description") or raw.get("payee") or "").strip()
    payee = str(raw.get("payee") or "").strip()

    return Transaction(
        transaction_id=txn_id,
        date=date,
        posted_date=None if pending else posted_date,
        account=account.label,
        description=description,
        payee=payee,
        amount=_to_decimal(raw.get("amount", "0"), "amount", txn_id),
        pending=pending,
    )


class SimpleFinClient:
    """Thin client over the SimpleFIN access URL. One request per date window."""

    def __init__(self, access_url: str, timeout: int = 60) -> None:
        self._url, self._auth = _split_credentials(access_url)
        self._timeout = timeout

    def _get(self, start: dt.date, end: dt.date) -> dict:
        query = urllib.parse.urlencode(
            {
                # version=2 is required for the current response format.
                "version": 2,
                # Pending transactions are EXCLUDED by default. Without this a
                # recent purchase is invisible until the bank posts it, which
                # can take days.
                "pending": 1,
                "start-date": int(
                    dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC).timestamp()
                ),
                "end-date": int(
                    dt.datetime.combine(end, dt.time.max, tzinfo=dt.UTC).timestamp()
                ),
            }
        )
        url = f"{self._url.rstrip('/')}/accounts?{query}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if self._auth:
            request.add_header("Authorization", self._auth)

        logger.info("SimpleFIN request for %s..%s", start, end)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            # Never include the URL: it may still be reconstructible from logs.
            raise SimpleFinError(f"SimpleFIN returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SimpleFinError(f"SimpleFIN unreachable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise SimpleFinError("SimpleFIN returned invalid JSON") from exc

    def fetch_transactions(self, start: dt.date, end: dt.date) -> FetchResult:
        """Fetch everything in `start..end`, paging by the 60-day cap.

        Transactions are de-duplicated by id across windows, since overlapping
        boundaries can legitimately return the same transaction twice.
        """
        windows = date_windows(start, end)
        if len(windows) > DAILY_REQUEST_LIMIT:
            raise SimpleFinError(
                f"{len(windows)} requests needed, over the {DAILY_REQUEST_LIMIT}/day limit"
            )

        accounts: dict[str, Account] = {}
        transactions: dict[str, Transaction] = {}
        errors: list[str] = []
        messages: list[str] = []

        for window_start, window_end in windows:
            result = parse_accounts(self._get(window_start, window_end))
            for account in result.accounts:
                accounts[account.id] = account
            for txn in result.transactions:
                transactions[txn.transaction_id] = txn
            errors.extend(result.errors)
            for message in result.messages:
                if message not in messages:
                    messages.append(message)

        return FetchResult(
            accounts=list(accounts.values()),
            transactions=sorted(transactions.values(), key=lambda t: (t.date, t.transaction_id)),
            errors=errors,
            requests_made=len(windows),
            messages=messages,
        )
