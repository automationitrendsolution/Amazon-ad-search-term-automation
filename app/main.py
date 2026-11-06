"""Pipeline orchestration and execution modes.

    check gen_ai_db.searchtermreport      (before any browser work)
      -> search_term_Automation (period Today) every run, plus the optional
         Yesterday report in the first hours of the day:
           reporting/history: Actions > Run, wait for the new run's Download
           -> validate the CSV (32 columns, dates, unique rows)
           -> one transaction, by DB_LOAD_MODE / --load-mode:
                replace: DELETE the file's date/marketplace pairs + INSERT -> COMMIT
                upsert:  INSERT ... ON DUPLICATE KEY UPDATE (on row_key) -> COMMIT
                insert:  INSERT new rows only, existing rows unchanged -> COMMIT
           -> publish data/reports/search_term_<kind>.csv, archive the download

Nothing is deleted from MySQL unless a validated report for that date and
marketplace is ready to insert in the same transaction.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar

from playwright.sync_api import Error as PlaywrightError

from .auth import sign_in, sign_in_interactive
from .browser import BrowserSession, safe_url
from .config import LOAD_MODES, REPORT_ID_PATTERN, TARGET_DATABASE, TARGET_TABLE, Settings
from .database import SearchTermDatabase, build_database
from .downloader import DownloadResult
from .exceptions import (
    AdFlowError,
    AuthenticationError,
    ConfigurationError,
    DatabaseError,
    NavigationError,
    ReportIdError,
    ReportsNotSavedError,
)
from .logging_config import get_logger
from .maintenance import cleanup_old_files
from .report_history import ReportHistoryPage
from .report_saver import (
    ResolvedReport,
    SavedReport,
    calendar_today,
    published_name,
    publish,
    reports_for_now,
    reports_for_run,
    yesterday_due,
)
from .retry import retry_call
from .runlock import run_lock
from .search_term_processor import TABLE_COLUMNS, read_search_term_report, transform_search_term_report
from .timeutils import now_in, now_utc

logger = get_logger("pipeline")

T = TypeVar("T")
DESTINATION = f"{TARGET_DATABASE}.{TARGET_TABLE}"


class RunMode(str, Enum):
    LOAD = "load"
    SAVE_REPORTS = "save-reports"
    PROCESS_FILE = "process-file"
    CREATE_TABLE = "create-table"
    LOGIN = "login"
    CHECK_CONFIG = "check-config"
    INSPECT_PAGE = "inspect-page"


@dataclass
class RunOptions:
    mode: RunMode = RunMode.LOAD
    dry_run: bool = False
    headed: bool | None = None
    process_file: Path | None = None
    report_id: str | None = None
    use_lock: bool = True
    echo_sql: bool = False
    inspect_menu: bool = False
    #: --login without a terminal: wait for the browser instead of Enter.
    no_prompt: bool = False
    #: --load-mode: overrides DB_LOAD_MODE for this run.
    load_mode: str | None = None
    #: --latest: download the newest finished run instead of clicking Run.
    latest: bool = False

    @property
    def writes_database(self) -> bool:
        return not self.dry_run and self.mode in {RunMode.LOAD, RunMode.PROCESS_FILE, RunMode.CREATE_TABLE}

    @property
    def uses_database(self) -> bool:
        return self.mode in {RunMode.LOAD, RunMode.PROCESS_FILE, RunMode.CREATE_TABLE}

    @property
    def label(self) -> str:
        return f"{self.mode.value} (dry-run)" if self.dry_run else self.mode.value


@dataclass
class RunSummary:
    mode: str
    started_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None
    report: ResolvedReport | None = None
    saved_reports: list[SavedReport] = field(default_factory=list)
    error: str | None = None
    succeeded: bool = False

    def log(self) -> None:
        end = self.finished_at or now_utc()
        logger.info("=" * 72)
        logger.info("Run summary (%s)", self.mode)
        logger.info("  started / finished : %s / %s (UTC)", self.started_at, end)
        logger.info("  duration           : %.1fs", (end - self.started_at).total_seconds())
        if self.report:
            logger.info("  report id          : %s (%s)", self.report.report_id, self.report.source)
        for saved in self.saved_reports:
            if saved.error:
                logger.info("  %-9s report     : NOT loaded -- %s", saved.kind, saved.error)
                continue
            logger.info("  %-9s report     : %s (%d rows)", saved.kind, saved.saved_to or "file", saved.rows)
            if saved.coverage:
                logger.info("  %-9s covers     : %s", saved.kind, saved.coverage)
            if saved.write:
                logger.info("  %-9s %-11s: %s", saved.kind, TARGET_TABLE[:11], saved.write.describe())
        if not any(s.write for s in self.saved_reports):
            logger.info("  database           : not modified")
        logger.info("  status             : %s", "SUCCESS" if self.succeeded else "FAILED")
        if self.error:
            logger.info("  error              : %s", self.error)
        logger.info("=" * 72)


class SearchTermPipeline:
    """One execution end to end."""

    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        *,
        database_factory: Callable[[], SearchTermDatabase] | None = None,
        download_fn: Callable[[ResolvedReport], DownloadResult] | None = None,
        reference_time: datetime | None = None,
    ) -> None:
        """
        Args:
            database_factory: Overrides how the MySQL connection is built (tests).
            download_fn: Overrides the browser download stage (tests).
            reference_time: Pretend "now" (tests).
        """
        self.settings = settings
        self.options = options
        self.summary = RunSummary(mode=options.label)
        self.load_mode = options.load_mode or settings.load_mode
        self.latest = options.latest or settings.download_latest
        if self.load_mode not in LOAD_MODES:
            raise ConfigurationError(f"Load mode must be one of {', '.join(LOAD_MODES)}, got {self.load_mode!r}")
        self._reference_time = reference_time
        self._owns_database = database_factory is None
        self._database_factory = database_factory or (
            lambda: build_database(
                settings.database,
                TABLE_COLUMNS,
                chunk_size=settings.insert_chunk_size,
                commit_every=settings.commit_every_rows,
                echo=options.echo_sql,
                validate=options.mode is not RunMode.CREATE_TABLE,
                load_mode=self.load_mode,
            )
        )
        self._download_fn = download_fn or self._browser_download

    # -- entry point -------------------------------------------------------

    def run(self) -> RunSummary:
        self.settings.ensure_directories()
        self._log_start()
        try:
            with run_lock(self.settings.lock_file, enabled=self.options.use_lock):
                {
                    RunMode.LOAD: lambda: self._run_reports(load_database=True),
                    RunMode.SAVE_REPORTS: lambda: self._run_reports(load_database=False),
                    RunMode.PROCESS_FILE: self._run_process_file,
                    RunMode.CREATE_TABLE: self._run_create_table,
                    RunMode.LOGIN: self._run_login,
                    RunMode.CHECK_CONFIG: self._run_check_config,
                    RunMode.INSPECT_PAGE: self._run_inspect_page,
                }[self.options.mode]()
            self.summary.succeeded = True
        except AdFlowError as exc:
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.error("Run failed: %s", self.summary.error)
            raise
        except Exception as exc:
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Unexpected failure")
            raise
        finally:
            self.summary.finished_at = now_utc()
            self.summary.log()
        return self.summary

    def _log_start(self) -> None:
        logger.info("=" * 72)
        logger.info("Search term automation starting | mode=%s", self.options.label)
        logger.info("  UTC            : %s", now_utc().isoformat(timespec="seconds"))
        for zone in (self.settings.report_timezone, self.settings.amazon_account_timezone):
            logger.info("  %-14s : %s", str(zone), now_in(zone, reference=self._reference_time).isoformat(timespec="seconds"))
        described = self.settings.describe()
        described["load_mode"] = self.load_mode
        if not self.options.uses_database:
            described["table"] = "(none: no MySQL in this mode)"
            del described["load_mode"]
        for key, value in described.items():
            logger.info("  %-14s : %s", key, value)
        logger.info("=" * 72)

    # -- modes -------------------------------------------------------------

    def _run_reports(self, *, load_database: bool) -> None:
        """Download the selected report(s), validate each, load it, publish the CSV.

        Each report is independent: one failing does not stop the other, but
        any failure makes the run exit nonzero.
        """
        reports = reports_for_run(self.settings, reference=self._reference_time)
        names = " + ".join(r.kind.capitalize() for r in reports)
        if load_database:
            logger.info(
                "Load (%s%s): %s -> %s", self.load_mode, ", download latest" if self.latest else "", names, DESTINATION
            )
        else:
            logger.info("Save-reports: %s; MySQL is not used", names)
        database = self._open_database() if load_database else None
        try:
            self._each_report(reports, database, load_database)
        finally:
            if database is not None and self._owns_database:
                database.dispose()

    def _each_report(
        self, reports: list[ResolvedReport], database: SearchTermDatabase | None, load_database: bool
    ) -> None:
        results = []
        for report in reports:
            outcome = SavedReport(report.kind, report.report_id)
            results.append(outcome)
            logger.info("--- %s report %s", report.kind, report.report_id)
            download = None
            try:
                download = self._retry(f"{report.kind} report download", lambda r=report: self._download_fn(r))
                # The latest finished run may cover any period (e.g. Last week),
                # so its dates are not held to MAX_REPORT_AGE_DAYS.
                self._load(download.path, report.report_id, outcome, database if load_database else None,
                           write=load_database, check_dates=not self.latest)
                target = self.settings.reports_dir / published_name(report.kind)
                outcome.saved_to = publish(download.path, target)
                self._move(download.path, self.settings.archive_dir)
                logger.info("Saved %s report: %s (%d row(s))", report.kind, outcome.saved_to, outcome.rows)
            except AdFlowError as exc:
                outcome.error = f"{type(exc).__name__}: {exc}"
                logger.error("%s report not loaded: %s", report.kind, outcome.error)
                if download is not None and download.path.exists():
                    self._move(download.path, self.settings.failed_dir)
                if isinstance(exc, (AuthenticationError, ConfigurationError)):
                    break  # the other report would fail the same way
        self.summary.saved_reports = results
        try:
            cleanup_old_files(self.settings)
        except Exception:
            logger.warning("Housekeeping failed", exc_info=True)
        failed = [r for r in results if r.error]
        if failed or len(results) < len(reports):
            first = next((r.error for r in failed), "a report was not attempted")
            raise ReportsNotSavedError(
                f"{len(results) - len(failed)} of {len(reports)} report(s) loaded; first error: {first}"
            )

    def _load(
        self,
        path: Path,
        report_id: str,
        outcome: SavedReport,
        database: SearchTermDatabase | None,
        *,
        write: bool = True,
        check_dates: bool = True,
    ) -> None:
        """Validate one CSV and replace its date/marketplace pairs in the table.

        ``check_dates`` refuses rows older than MAX_REPORT_AGE_DAYS (a stale
        run); --process-file backfills turn it off.
        """
        report = transform_search_term_report(
            read_search_term_report(path),
            report_id=report_id,
            loaded_at=now_utc(),
            today=calendar_today(self.settings, reference=self._reference_time) if check_dates else None,
            max_age_days=self.settings.max_report_age_days,
            csv_date_format=self.settings.csv_date_format,
        )
        outcome.rows = report.row_count
        outcome.coverage = report.describe()
        if report.row_count == 0:
            # Never a delete-only write: existing rows are kept.
            logger.warning("%s report has no rows yet; existing rows kept", outcome.kind)
            return
        if not write:
            return
        if database is None or not self.options.writes_database:
            logger.info(
                "DRY RUN: %d rows prepared for a %s load (%s); nothing written",
                report.row_count, self.load_mode, outcome.coverage,
            )
            return
        records = database.lazy_records(report.frame)
        outcome.write = self._retry(
            f"{outcome.kind} database {self.load_mode}",
            lambda: database.write(self.load_mode, report.slices, records),
        )

    def _run_process_file(self) -> None:
        """Load a Search term CSV already on disk (e.g. a backfill).  The file is never moved."""
        assert self.options.process_file is not None
        source = Path(self.options.process_file).expanduser().resolve()
        outcome = SavedReport("file", "-", saved_to=source)
        self.summary.saved_reports = [outcome]
        database = self._open_database()
        try:
            self._load(source, self.options.report_id or "", outcome, database, check_dates=False)
        finally:
            if database is not None and self._owns_database:
                database.dispose()

    def _run_create_table(self) -> None:
        """CREATE TABLE IF NOT EXISTS from sql/create_searchtermreport.sql, then validate it."""
        if self.options.dry_run:
            logger.info("DRY RUN: the table would be created from sql/create_searchtermreport.sql")
            return
        database = self._retry("Database connection", self._database_factory)
        try:
            created = database.create_table()
            database.reflect()
            database.validate_schema(TABLE_COLUMNS, self.load_mode)
            logger.info("%s %s and matches the loader", DESTINATION, "created" if created else "already exists")
        finally:
            if self._owns_database:
                database.dispose()

    def _run_check_config(self) -> None:
        """Validate configuration and report IDs only."""
        for report in reports_for_now(self.settings, reference=self._reference_time):
            logger.info("%s report: %s", report.kind, report.report_id)
            logger.info("  history URL: %s", self.settings.history_url(report.report_id))
        due = self.settings.report_id_yesterday and yesterday_due(self.settings, reference=self._reference_time)
        logger.info("A run now would fetch: Today%s", " + Yesterday" if due else "")
        self.summary.report = self._page_report()
        logger.info("Configuration OK. No browser opened, no database contacted.")

    def _run_inspect_page(self) -> None:
        """Open the history page and test the selectors without clicking anything."""
        logger.info("Inspect-page: read-only, nothing is clicked; MySQL is not used")
        report = self._page_report()
        self.summary.report = report
        headless = self.settings.headless if self.options.headed is None else not self.options.headed
        with BrowserSession(self.settings, headless=headless) as session:
            details = ReportHistoryPage(session, self.settings).inspect(
                report, open_actions_menu=self.options.inspect_menu
            )
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        target = self.settings.screenshot_dir / f"{stamp}_inspect-report-history.json"
        target.write_text(json.dumps(details, indent=2, ensure_ascii=False), encoding="utf-8")
        for key, results in details["selectors"].items():  # type: ignore[union-attr]
            hit = next((r["selector"] for r in results if r.get("first_visible") is True), None)
            logger.info("  %-45s %s", key, f"OK via {hit!r}" if hit else "NO VISIBLE MATCH")
        logger.info("History rows matched: %s", details["history_rows_matched"])
        if details.get("actions_menu_items") is not None:
            logger.info("Actions menu offers: %s", details["actions_menu_items"])
            hit = next(
                (r["selector"] for r in details["run_report_menu_item"] or [] if r.get("first_visible") is True),
                None,
            )
            logger.info("  %-45s %s", "run_report_menu_item", f"OK via {hit!r}" if hit else "NO VISIBLE MATCH")
        logger.info("Page description written to %s", target)
        logger.info("Screenshot: %s", details["screenshot"])

    # -- stages ------------------------------------------------------------

    def _page_report(self) -> ResolvedReport:
        """The report opened by --login / --inspect-page: --report-id, else Today's."""
        if self.options.report_id:
            report_id = self.options.report_id.strip().lower()
            if not REPORT_ID_PATTERN.match(report_id):
                raise ReportIdError(f"--report-id {self.options.report_id!r} is not a report ID (UUID)")
            return ResolvedReport(report_id, calendar_today(self.settings, reference=self._reference_time), "cli")
        reports = reports_for_now(self.settings, reference=self._reference_time)
        if not reports:
            raise ConfigurationError("Set REPORT_ID_TODAY, or pass --report-id")
        return reports[0]

    def _open_database(self) -> SearchTermDatabase | None:
        try:
            return self._retry("Database connection", self._database_factory)
        except DatabaseError:
            if self.options.writes_database:
                raise
            logger.warning("DRY RUN: MySQL unavailable; continuing without schema validation")
            return None

    def _browser_download(self, report: ResolvedReport) -> DownloadResult:
        """One attempt: open, run, wait for completion, download."""
        headless = self.settings.headless if self.options.headed is None else not self.options.headed
        with BrowserSession(self.settings, headless=headless) as session:
            history = ReportHistoryPage(session, self.settings)
            history.open(report)
            if self.latest:
                logger.info("Download latest: taking the newest finished run; Run is not clicked")
            else:
                before = history.run_report()
                history.wait_for_completion(before)
            result = history.download()
            session.save_state()  # signed in: refresh the session backup
            return result

    def _retry(self, description: str, operation: Callable[[], T]) -> T:
        return retry_call(
            operation,
            description=description,
            attempts=self.settings.max_retries,
            base_delay=self.settings.retry_backoff_seconds,
            max_delay=self.settings.retry_backoff_max_seconds,
        )

    def _move(self, path: Path, directory: Path) -> Path | None:
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / path.name
        try:
            shutil.move(str(path), str(destination))
            logger.info("Moved report to %s", destination)
            return destination
        except OSError as exc:
            logger.warning("Could not move %s to %s: %s", path, directory, exc)
            return None

    # -- interactive login -------------------------------------------------

    def _run_login(self) -> None:
        """Establish the Amazon session with a person at the keyboard.

        With a display (always on Windows / macOS, or ``--headed``) a Chrome
        window opens: the email and password are filled in, the person enters
        the OTP, and the session is saved the moment the report page shows.
        Closing the window at any point is fine.  Afterwards the saved session
        is checked in the same headless browser the scheduled runs use.

        Without a display it signs in headless and asks for the OTP at the
        terminal prompt.  CAPTCHAs need a display.
        """
        report = self._page_report()
        headed = bool(
            self.options.headed
            or os.name == "nt"
            or sys.platform == "darwin"
            or os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
        )
        if headed:
            self._sign_in_in_window(report)
        else:
            with BrowserSession(self.settings, headless=True) as session:
                history = ReportHistoryPage(session, self.settings)
                history.report = report
                history._goto()
                if history.is_showing_sign_in():
                    sign_in_interactive(session, self.settings)
                    if not history.is_on_history_page():
                        history._goto()
                history.assert_authenticated()
                session.save_state()
        self._verify_session(report)

    def _sign_in_in_window(self, report: ResolvedReport, timeout_s: int = 900) -> None:
        """Open a visible window and save the session as soon as it is signed in."""
        print("\nA Chrome window is opening. Sign in there and enter the OTP code if Amazon asks.")
        print("The session is saved as soon as the report page shows; you can then close the window.\n",
              flush=True)
        with BrowserSession(self.settings, headless=False) as session:
            history = ReportHistoryPage(session, self.settings)
            history.report = report
            try:
                history._goto()
                if history.is_showing_sign_in() and self.settings.has_credentials:
                    try:
                        sign_in(session, self.settings)
                    except AuthenticationError as exc:
                        print(f"Finish the sign-in in the window: {exc}\n", flush=True)
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline and not session.page.is_closed():
                    try:
                        if history.is_on_history_page() and not history.is_showing_sign_in():
                            session.save_state()
                            try:
                                history.wait_until_loaded()
                            except NavigationError:
                                pass
                            session.save_state()
                            print("  OK: signed in -- session saved. The window closes now.", flush=True)
                            return
                        session.page.wait_for_timeout(3_000)
                    except PlaywrightError:
                        if session.page.is_closed():
                            break
                        session.page.wait_for_timeout(1_000)  # mid-navigation; look again
                else:
                    if not session.page.is_closed():
                        print(f"  Not signed in after {timeout_s // 60} minutes; closing the window.", flush=True)
            except PlaywrightError:
                if not session.page.is_closed():
                    raise
            if session.page.is_closed():
                print("  The window was closed; checking whether the sign-in was saved...", flush=True)

    def _verify_session(self, report: ResolvedReport) -> None:
        """Open the report in the headless browser the scheduled runs use."""
        print("\nChecking the saved session the way the scheduled runs will use it (hidden browser)...",
              flush=True)
        with BrowserSession(self.settings, headless=True) as session:
            history = ReportHistoryPage(session, self.settings)
            history.report = report
            history._goto()
            history.assert_authenticated()  # raises AuthenticationError with a screenshot
            if not history.is_on_history_page():
                raise AuthenticationError(
                    f"Signed in, but Amazon did not open the report history page "
                    f"({safe_url(session.page.url)}). Check REPORT_ID_TODAY, then run --login again."
                )
            session.save_state()
        logger.info("Session verified; saved in %s and %s", self.settings.session_dir, session.state_file)
        print("  OK: the saved session works -- scheduled runs can open the report.")
        print(f"\nSession stored in {self.settings.session_dir}. Next: python run.py --inspect-page\n")


def handle_failure(pipeline: SearchTermPipeline, error: BaseException) -> None:
    """Operator-facing guidance for a failed run."""
    if isinstance(error, AuthenticationError) or (
        isinstance(error, ReportsNotSavedError) and "AuthenticationError" in str(error)
    ):
        logger.error("Authentication needs a person. Run 'python run.py --login' to refresh the session.")
    elif isinstance(error, DatabaseError):
        logger.error("Nothing was committed; existing rows in %s are intact.", DESTINATION)
    elif isinstance(error, ReportsNotSavedError):
        logger.error("A report that failed changed nothing in %s; see the summary above.", DESTINATION)
