"""Command-line entry point: sync | backfill | review."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .config import Config, ConfigError
from .models import Transaction
from .simplefin import SimpleFinClient, SimpleFinError

logger = logging.getLogger("finance_sync")

BACKFILL_MAX_DAYS = 90


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _print_transactions(transactions: list[Transaction]) -> None:
    """Stage 1 checkpoint output: the parsed ledger, grouped by target month."""
    if not transactions:
        print("No transactions in range.")
        return

    by_month: dict[str, list[Transaction]] = {}
    for txn in transactions:
        by_month.setdefault(txn.month_key, []).append(txn)

    for month in sorted(by_month):
        rows = by_month[month]
        print(f"\n=== Finances {month}  ({len(rows)} transactions) ===")
        print(f"{'date':<11} {'amount':>11}  {'status':<8} {'account':<26} description")
        print("-" * 100)
        for txn in rows:
            status = "pending" if txn.pending else "posted"
            print(
                f"{txn.date.isoformat():<11} {txn.amount:>11} "
                f" {status:<8} {txn.account[:26]:<26} {txn.description[:40]}"
            )
        total = sum(t.amount for t in rows)
        print(f"{'':<11} {total:>11}  (net)")


def _date_range(days: int) -> tuple[dt.date, dt.date]:
    end = dt.date.today()
    return end - dt.timedelta(days=days - 1), end


def _run_fetch(config: Config, days: int) -> int:
    client = SimpleFinClient(config.access_url)
    start, end = _date_range(days)
    logger.info("Fetching %s..%s", start, end)

    result = client.fetch_transactions(start, end)

    for message in result.errors:
        logger.error("SimpleFIN error: %s", message)

    _print_transactions(result.transactions)
    print(
        f"\nfetched={len(result.transactions)} accounts={len(result.accounts)} "
        f"requests={result.requests_made} errors={len(result.errors)}"
    )

    # A non-empty errors array must make a scheduled run fail visibly.
    return 1 if result.errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finance-sync", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sync = sub.add_parser("sync", help="sync the current month (default)")
    sync.add_argument("--days", type=int, default=30, help="lookback window")
    sync.add_argument("--dry-run", action="store_true", help="print, never write")

    backfill = sub.add_parser("backfill", help="pull historical transactions")
    backfill.add_argument("--days", type=int, default=BACKFILL_MAX_DAYS)
    backfill.add_argument("--dry-run", action="store_true")

    review = sub.add_parser("review", help="process hand-categorized Review rows")
    review.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    command = args.command or "sync"

    try:
        config = Config.load()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2

    try:
        if command == "backfill":
            days = min(args.days, BACKFILL_MAX_DAYS)
            if days != args.days:
                logger.warning("Capping backfill at %s days", BACKFILL_MAX_DAYS)
            return _run_fetch(config, days)
        if command == "sync":
            return _run_fetch(config, args.days)
        if command == "review":
            logger.error("`review` arrives in stage 5")
            return 2
    except SimpleFinError as exc:
        logger.error("%s", exc)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
