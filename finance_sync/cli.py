"""Command-line entry point: sync | backfill | review."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .config import Config, ConfigError
from .errors import explain_http_error
from .models import Transaction
from .sheets import SheetsSync
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
    """The parsed ledger, grouped by the spreadsheet it will be routed to."""
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


def _print_category_breakdown(plans) -> None:
    """Show where this run's categories came from, so gaps are obvious."""
    from .rows import TRANSACTION_COLUMNS

    cat_i = TRANSACTION_COLUMNS.index("category")
    src_i = TRANSACTION_COLUMNS.index("category_source")

    counts: dict[tuple[str, str], int] = {}
    for plan in plans.values():
        rows = list(plan.inserts) + [row for _, row in plan.updates]
        for row in rows:
            key = (row[cat_i], row[src_i])
            counts[key] = counts.get(key, 0) + 1

    if not counts:
        return
    print("\ncategories written this run:")
    for (category, source), count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {category:<14} {source:<10} {count}")
    unresolved = sum(c for (_, src), c in counts.items() if src == "none")
    if unresolved:
        print(f"  -> {unresolved} unresolved; add Overrides rows or enable USE_JEV")


def _print_budget(sheets, plans) -> None:
    """Show Planned vs Actual for each month this run touched."""
    from .budget import BUDGET_COLUMNS

    for month_key in sorted(plans):
        spreadsheet_id = sheets.find_spreadsheet(month_key)
        if spreadsheet_id is None:
            continue
        rows = sheets.budget_rows(spreadsheet_id)
        if not rows:
            continue
        p_i = BUDGET_COLUMNS.index("planned")
        a_i = BUDGET_COLUMNS.index("actual")
        d_i = BUDGET_COLUMNS.index("difference")

        print(f"\nBudget {month_key}:")
        print(f"  {'line':<20} {'planned':>10} {'actual':>10} {'diff':>10}")
        print("  " + "-" * 52)
        for row in rows:
            label = row[1]
            marker = "  <<" if label != "Totals" and float(row[d_i] or 0) > 0 else ""
            print(f"  {label:<20} {row[p_i]:>10} {row[a_i]:>10} {row[d_i]:>10}{marker}")
            if label == "Totals":
                print("  " + "-" * 52)


def _date_range(days: int) -> tuple[dt.date, dt.date]:
    end = dt.date.today()
    return end - dt.timedelta(days=days - 1), end


def _make_backend(config: Config, fake: bool):
    """Pick a Sheets transport. `--fake-sheets` rehearses with no credentials."""
    if fake:
        from .fake_backend import FakeSheetsBackend

        logger.warning("Using an in-memory fake Sheets backend; nothing is persisted")
        return FakeSheetsBackend()

    missing = [
        name
        for name, value in (
            ("GOOGLE_CLIENT_ID", config.google_client_id),
            ("GOOGLE_CLIENT_SECRET", config.google_client_secret),
            ("GOOGLE_REFRESH_TOKEN", config.google_refresh_token),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"Missing Google credentials: {', '.join(missing)}. "
            "Run scripts/auth.py once to mint a refresh token."
        )

    from .google_backend import GoogleSheetsBackend, build_credentials

    return GoogleSheetsBackend(
        build_credentials(
            config.google_client_id,  # type: ignore[arg-type]
            config.google_client_secret,  # type: ignore[arg-type]
            config.google_refresh_token,  # type: ignore[arg-type]
        )
    )


def _run_sync(config: Config, args, days: int) -> int:
    client = SimpleFinClient(config.access_url)
    start, end = _date_range(days)
    logger.info("Fetching %s..%s", start, end)

    result = client.fetch_transactions(start, end)
    for message in result.messages:
        logger.info("SimpleFIN says: %s", message)
    for message in result.errors:
        logger.error("SimpleFIN error: %s", message)

    _print_transactions(result.transactions)

    if args.print_only:
        print(
            f"\nfetched={len(result.transactions)} accounts={len(result.accounts)} "
            f"requests={result.requests_made} errors={len(result.errors)}"
        )
        return 1 if result.errors else 0

    backend = _make_backend(config, args.fake_sheets)
    sheets = SheetsSync(backend, folder_name=config.drive_folder_name, dry_run=args.dry_run)

    def categorizer_factory(overrides):
        from .categorize import LayeredCategorizer
        from .jev import build_categorizer

        return LayeredCategorizer(overrides=overrides, fallback=build_categorizer(config))

    plans = sheets.sync(result.transactions, categorizer_factory=categorizer_factory)

    totals = {"inserted": 0, "updated": 0, "unchanged": 0, "routed_to_review": 0}
    print()
    for month_key, plan in sorted(plans.items()):
        print(f"Finances {month_key}:")
        print(plan.describe())
        for key in totals:
            totals[key] += plan.counts[key]

    _print_category_breakdown(plans)
    if not args.dry_run:
        _print_budget(sheets, plans)

    verb = "would change" if args.dry_run else "applied"
    print(
        f"\n{verb}: fetched={len(result.transactions)} inserted={totals['inserted']} "
        f"updated={totals['updated']} unchanged={totals['unchanged']} "
        f"routed_to_review={totals['routed_to_review']} "
        f"requests={result.requests_made} errors={len(result.errors)}"
    )

    # A non-empty errors array must make a scheduled run fail visibly.
    return 1 if result.errors else 0


def _run_review(config: Config, args) -> int:
    """Fold hand-categorized Review rows back into Transactions."""
    from .review import process_rows

    backend = _make_backend(config, args.fake_sheets)
    sheets = SheetsSync(backend, folder_name=config.drive_folder_name, dry_run=args.dry_run)

    months = sheets.month_keys()
    if not months:
        print("No Finances spreadsheets found.")
        return 0

    totals = {"promoted": 0, "overrides_added": 0, "still_pending": 0, "skipped": 0}
    for month_key in months:
        rows = sheets.review_rows(month_key)
        if not rows:
            continue
        outcome = process_rows(rows, sheets.override_rows(month_key))
        outcome.month_key = month_key

        if not outcome.promoted and not outcome.skipped:
            totals["still_pending"] += len(outcome.remaining)
            continue

        print(f"\nFinances {month_key}:")
        for txn in outcome.promoted:
            print(f"  -> {txn.category.value:<18} {txn.description[:44]}")
        for pattern, category, _ in outcome.new_overrides:
            print(f"     override added: {pattern!r} -> {category}")
        if outcome.skipped:
            print(f"  !! {len(outcome.skipped)} row(s) unusable, left on the tab")

        sheets.apply_review(month_key, outcome)
        for key in totals:
            totals[key] += outcome.counts[key]

    verb = "would promote" if args.dry_run else "promoted"
    print(
        f"\n{verb}: {totals['promoted']} "
        f"overrides_added={totals['overrides_added']} "
        f"still_pending={totals['still_pending']} skipped={totals['skipped']}"
    )
    return 0


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    parser.add_argument("--print-only", action="store_true", help="print transactions and skip Sheets entirely")
    parser.add_argument("--fake-sheets", action="store_true", help="rehearse against an in-memory sheet")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finance-sync", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sync = sub.add_parser("sync", help="sync recent transactions (default)")
    sync.add_argument("--days", type=int, default=30, help="lookback window")
    _add_common_flags(sync)

    backfill = sub.add_parser("backfill", help="pull historical transactions")
    backfill.add_argument("--days", type=int, default=BACKFILL_MAX_DAYS)
    _add_common_flags(backfill)

    review = sub.add_parser("review", help="process hand-categorized Review rows")
    _add_common_flags(review)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    command = args.command or "sync"
    if args.command is None:
        args = parser.parse_args(["sync"])

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
            return _run_sync(config, args, days)
        if command == "sync":
            return _run_sync(config, args, args.days)
        if command == "review":
            return _run_review(config, args)
    except (SimpleFinError, ConfigError) as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-chance translation
        explanation = explain_http_error(exc)
        if explanation is None:
            raise
        logger.error("%s", explanation)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
