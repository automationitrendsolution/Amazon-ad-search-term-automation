#!/usr/bin/env python3
"""Search term automation command-line entry point.

Runs the saved Amazon Ads report search_term_Automation on reporting/history
(Actions > Run), waits for the new run's Download, downloads the CSV and
replaces its dates/marketplaces in gen_ai_db.searchtermreport.

    python run.py --create-table          # one-time: create gen_ai_db.searchtermreport
    python run.py --load                  # report(s) -> files + MySQL (what cron calls)
    python run.py --load --dry-run        # full workflow, no MySQL writes
    python run.py --save-reports          # report(s) -> files only
    python run.py --process-file r.csv    # load a Search term CSV without opening Amazon
    python run.py --load --load-mode upsert   # replace | upsert | insert (default: DB_LOAD_MODE)
    python run.py --check-config          # validate config and report IDs only
    python run.py --login                 # one-time Amazon session setup
    python run.py --inspect-page          # test selectors on the live page, no clicks

Exit codes: 0 success, 2 config, 3 authentication (needs a person),
4 navigation, 6 download, 7 CSV, 8 transform, 9 database, 11 locked,
12 report ID, 13 report not ready, 14 date mismatch, 16 not every report loaded.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import __version__
from app.config import LOAD_MODES, load_settings
from app.exceptions import AdFlowError, ConfigurationError
from app.logging_config import configure_logging, get_logger
from app.main import RunMode, RunOptions, SearchTermPipeline, handle_failure

_EXIT_CODES = """exit codes:
  0 success, 2 config, 3 authentication (needs a person), 4 navigation,
  6 download, 7 CSV, 8 transform, 9 database, 11 locked, 12 report ID,
  13 report not ready, 14 date mismatch,
  16 --load / --save-reports: not every report was loaded
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="search-term-automation",
        description=(
            "Run the Amazon Ads search_term_Automation report, download the CSV and load it into "
            "gen_ai_db.searchtermreport."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EXIT_CODES,
    )
    parser.add_argument("--version", action="version", version=f"search-term-automation {__version__}")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except write to MySQL.")

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--load", action="store_true",
                       help="Run + download the report(s), save, and replace their dates in the table "
                            "(what cron runs; also the default).")
    modes.add_argument("--save-reports", action="store_true", help="Run + download the report(s) only (no MySQL).")
    modes.add_argument("--process-file", type=Path, metavar="PATH",
                       help="Load an existing Search term CSV without opening Amazon.")
    modes.add_argument("--create-table", action="store_true",
                       help="Create gen_ai_db.searchtermreport if it does not exist (one-time).")
    modes.add_argument("--check-config", action="store_true", help="Validate configuration; no browser or DB.")
    modes.add_argument("--login", action="store_true", help="One-time interactive Amazon sign-in.")
    modes.add_argument("--inspect-page", action="store_true",
                       help="Open the report history page read-only and test every selector.")

    parser.add_argument("--load-mode", choices=list(LOAD_MODES),
                        help="How rows are written (overrides DB_LOAD_MODE): replace = delete the file's "
                             "dates/marketplaces and insert; upsert = insert new rows and update existing "
                             "ones; insert = insert new rows only, keep existing ones unchanged.")
    parser.add_argument("--latest", action="store_true",
                        help="With --load / --save-reports: download the newest finished run (like "
                             "'Download latest' in Amazon) instead of clicking Run and waiting. "
                             "Same as DOWNLOAD_LATEST=true.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--inspect-menu", action="store_true",
                        help="With --inspect-page: also open the Actions dropdown and list it (chooses nothing).")
    parser.add_argument("--no-prompt", action="store_true",
                        help="With --login --headed: wait (up to 10 min) for the sign-in in the browser "
                             "instead of Enter.")
    parser.add_argument("--report-id", metavar="UUID",
                        help="With --login / --inspect-page: open this report; with --process-file: "
                             "stored in report_id.")
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--no-lock", action="store_true", help="Skip the run lock (supervised runs only).")
    parser.add_argument("--echo-sql", action="store_true", help="Log SQL statements.")
    return parser


def resolve_mode(args: argparse.Namespace) -> RunMode:
    if args.login:
        return RunMode.LOGIN
    if args.check_config:
        return RunMode.CHECK_CONFIG
    if args.inspect_page:
        return RunMode.INSPECT_PAGE
    if args.save_reports:
        return RunMode.SAVE_REPORTS
    if args.create_table:
        return RunMode.CREATE_TABLE
    if args.process_file is not None:
        return RunMode.PROCESS_FILE
    return RunMode.LOAD


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ConfigurationError.exit_code

    settings.ensure_directories()
    configure_logging(
        settings.log_dir,
        level=args.log_level or settings.log_level,
        retention_days=settings.log_retention_days,
    )
    logger = get_logger("cli")

    if args.process_file is not None and not Path(args.process_file).expanduser().is_file():
        logger.error("File not found: %s", args.process_file)
        return 2

    options = RunOptions(
        mode=resolve_mode(args),
        dry_run=args.dry_run,
        headed=True if args.headed else None,
        process_file=args.process_file,
        report_id=args.report_id,
        use_lock=not args.no_lock,
        echo_sql=args.echo_sql,
        inspect_menu=args.inspect_menu,
        no_prompt=args.no_prompt,
        load_mode=args.load_mode,
        latest=args.latest,
    )
    pipeline = SearchTermPipeline(settings, options)
    try:
        pipeline.run()
    except AdFlowError as exc:
        handle_failure(pipeline, exc)
        return exc.exit_code
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    except Exception:  # already logged with traceback
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
