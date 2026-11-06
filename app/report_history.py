"""Amazon Ads reporting/history automation.

For one saved report definition this module:

1. opens ``reporting/history?reportId=...`` in the persistent, signed-in session;
2. records what the history table looks like, then clicks **Run report**;
3. reloads the page every REPORT_POLL_INTERVAL_SECONDS until the *new* run's
   status turns from ``-`` into a download action (or it fails, or times out);
4. downloads that run's CSV through the browser's download event.

A loaded page is never taken to mean a finished report: completion is only
accepted once the new run's row offers a download.

Security boundaries are those of :mod:`app.auth`: the configured credentials
may be filled in, but CAPTCHA, OTP and MFA prompts stop the run for a person.

The page's DOM was not available when this was written, so every locator lives
in ``config/selectors.json`` as an ordered candidate list.  Verify them once
with ``python run.py --inspect-page`` and put the working selector
at the top of each list.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from enum import Enum

from playwright.sync_api import Download, Locator, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .auth import sign_in
from .browser import BrowserSession, safe_url
from .config import Settings
from .downloader import DownloadResult, target_filename, validate_download
from .exceptions import (
    AuthenticationError,
    DownloadError,
    NavigationError,
    ReportIdError,
    ReportNotReadyError,
)
from .logging_config import get_logger
from .report_saver import ResolvedReport
from .selectors import candidates, find_first, is_present
from .timeutils import UTC, now_in

logger = get_logger("history")

_FAILED = re.compile(r"\bfail(ed|ure)?\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# completion detection -- pure logic, unit-tested without a browser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistorySnapshot:
    """Fingerprint of the history table: row count and the newest row's text."""

    row_count: int
    top_row: str

    #: Used when a run was already in progress: whatever is on top is "ours".
    @classmethod
    def nothing(cls) -> HistorySnapshot:
        return cls(row_count=-1, top_row="\x00")


class RunState(str, Enum):
    NOT_STARTED = "not-started"  # the new run's row has not appeared yet
    PENDING = "pending"  # row is there, status is still "-"
    READY = "ready"  # row offers a download
    FAILED = "failed"  # Amazon reported the run as failed


#: "Oct 22, 2026 9:01 PM" in a history row's "Date created" cell.
_ROW_CREATED = re.compile(r"\b([A-Z][a-z]{2} \d{1,2}, \d{4})\s+(\d{1,2}:\d{2})\s*([AP]M)\b")

#: A run created this long before our click still counts as ours: Amazon shows
#: minutes only, and the server clock may be slightly off.
_CREATED_TOLERANCE = timedelta(minutes=2)


def row_created_at(row_text: str, tz: tzinfo) -> datetime | None:
    """The "Date created" of a history row, in the account timezone Amazon displays."""
    match = _ROW_CREATED.search(row_text)
    if match is None:
        return None
    try:
        naive = datetime.strptime(f"{match.group(1)} {match.group(2)}{match.group(3)}", "%b %d, %Y %I:%M%p")
    except ValueError:
        return None
    return naive.replace(tzinfo=tz)


def is_older_than(row_text: str, started_after: datetime) -> bool:
    """True when the row's created time is readable and clearly before ``started_after``."""
    created = row_created_at(row_text, started_after.tzinfo or UTC)
    return created is not None and created < started_after - _CREATED_TOLERANCE


def classify_run(
    before: HistorySnapshot,
    now: HistorySnapshot,
    *,
    has_download: bool,
    started_after: datetime | None = None,
) -> RunState:
    """Decide where the run we started stands.

    The newest row only counts as the new run if the table changed since
    before the click -- more rows, or a different top row.  Otherwise the
    download link on top belongs to an *older* run and must not be used.

    With ``started_after`` (when we clicked Run), a row whose "Date created"
    is older than the click is never ours, even if the table changed: seen
    live (2026-10-23), right after the click the re-rendering table briefly
    put an Oct 20 run on top, and its Download was taken for the new run.
    """
    is_new = now.row_count > before.row_count or now.top_row != before.top_row
    if started_after is not None and is_older_than(now.top_row, started_after):
        is_new = False
    if now.row_count <= 0 or not is_new:
        return RunState.NOT_STARTED
    if _FAILED.search(now.top_row):
        return RunState.FAILED
    if has_download:
        return RunState.READY
    return RunState.PENDING


def poll_until_ready(
    probe: Callable[[], RunState],
    reload: Callable[[], None],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Reload until :func:`probe` reports READY; return the number of polls.

    Raises:
        ReportNotReadyError: Amazon marked the run failed, or it was still
            pending (or never appeared) when the timeout ran out.
    """
    deadline = clock() + timeout_seconds
    polls = 0
    while True:
        polls += 1
        state = probe()
        logger.info("Report status poll %d: %s", polls, state.value)
        if state is RunState.READY:
            return polls
        if state is RunState.FAILED:
            raise ReportNotReadyError("Amazon reported the report run as failed")
        if clock() + interval_seconds > deadline:
            raise ReportNotReadyError(
                f"Report still {state.value} after {timeout_seconds:.0f}s "
                f"({polls} poll(s)); status never changed from '-' to a download. "
                "Raise REPORT_READY_TIMEOUT_SECONDS if Amazon is slow today."
            )
        sleep(interval_seconds)
        reload()


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


class ReportHistoryPage:
    """Operations against one report's reporting/history page."""

    def __init__(self, session: BrowserSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.report: ResolvedReport | None = None
        #: When we clicked Run (account timezone); None when waiting on a run
        #: that was already in progress.
        self._clicked_at: datetime | None = None

    @property
    def page(self) -> Page:
        return self.session.page

    # -- navigation and authentication --------------------------------------

    def open(self, report: ResolvedReport) -> None:
        """Load the history page, signing in once if the session lapsed."""
        self.report = report
        self._goto()
        if self.is_showing_sign_in():
            if not self.settings.can_auto_login:
                self.session.capture_failure("sign-in-required")
                raise AuthenticationError(
                    "Amazon session is missing or expired (sign-in page shown), and "
                    "automatic sign-in is not configured. Set AMAZON_EMAIL and "
                    "AMAZON_PASSWORD, or re-authenticate with: python run.py --login"
                )
            sign_in(self.session, self.settings)
            self._goto()
        self.assert_authenticated()
        self.wait_until_loaded()
        self.assert_report_exists()
        logger.info("Report history loaded for report %s", report.report_id)

    def _navigate(self, action: Callable[[], object], what: str) -> None:
        """Run a goto/reload, following Amazon's own client-side redirect.

        The history page rewrites its URL as it boots (it appends
        ``ephemeralAdvertiser=true``), which Playwright reports as the
        navigation being "interrupted by another navigation".  That is the
        page arriving, not failing: wait for the redirected page instead.
        Where it lands is checked by the caller (sign-in, loaded marker).
        """
        try:
            action()
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"Timed out {what}: {exc}") from exc
        except PlaywrightError as exc:
            if "interrupted by another navigation" not in str(exc):
                raise NavigationError(f"Failed {what}: {exc}") from exc
            logger.info("Amazon redirected while %s; following it to %s", what, safe_url(self.page.url))
            try:
                self.page.wait_for_load_state(
                    "domcontentloaded", timeout=self.settings.browser_timeout_ms
                )
            except PlaywrightTimeoutError as wait_exc:
                raise NavigationError(f"Timed out after a redirect while {what}") from wait_exc

    def _goto(self) -> None:
        assert self.report is not None
        url = self.settings.history_url(self.report.report_id)
        logger.info("Opening report history for report %s", self.report.report_id)
        self._navigate(
            lambda: self.page.goto(url, wait_until="domcontentloaded"),
            "loading the report history page",
        )

    def is_on_history_page(self) -> bool:
        return "/reporting/history" in self.page.url.split("?", 1)[0]

    def reload(self) -> None:
        self._navigate(
            lambda: self.page.reload(wait_until="domcontentloaded"),
            "reloading the report history page",
        )
        self.assert_authenticated()
        self.wait_until_loaded()

    def is_showing_sign_in(self) -> bool:
        url = self.page.url.lower().split("?", 1)[0]
        if any(marker in url for marker in ("/ap/signin", "/ap/mfa", "/ap/cvf")):
            return True
        return is_present(self.page, "sign_in_marker", timeout_ms=1_500)

    def assert_authenticated(self) -> None:
        """Stop if Amazon shows sign-in or a CAPTCHA/OTP/MFA challenge."""
        if is_present(self.page, "security_challenge_marker", timeout_ms=1_500):
            self.session.capture_failure("security-challenge")
            raise AuthenticationError(
                "Amazon presented a security challenge (CAPTCHA/OTP/MFA). It is not "
                "answered automatically. Complete it once with 'python run.py --login'."
            )
        if self.is_showing_sign_in():
            self.session.capture_failure("sign-in-required")
            raise AuthenticationError(
                "Amazon is still showing the sign-in page. Re-authenticate with: "
                "python run.py --login"
            )

    def assert_no_page_error(self) -> None:
        """Stop on Amazon's own error panel ("Sorry, something went wrong").

        Seen on the live page (2026-10-20) in place of the history table.
        Transient: the retry reloads the page in a fresh browser.
        """
        if is_present(self.page, "report_error_marker", timeout_ms=500):
            self.session.capture_failure("amazon-page-error")
            raise NavigationError(
                "Amazon showed 'Sorry, something went wrong' instead of the report history. "
                "If this persists, check in a normal browser that this login can open the "
                "report and that the right advertiser account is selected."
            )

    def wait_until_loaded(self) -> None:
        deadline = time.monotonic() + self.settings.browser_timeout_ms / 1000
        marker = None
        while marker is None and time.monotonic() < deadline:
            self.assert_no_page_error()
            # Every candidate is checked on every pass (no per-candidate wait),
            # so a fallback is reached even when the first one never matches.
            for selector in candidates("report_history_loaded_marker"):
                try:
                    locator = self.page.locator(selector).first
                    if locator.is_visible():
                        marker = locator
                        break
                except Exception:
                    continue
            if marker is None:
                self.page.wait_for_timeout(500)
        if marker is None:
            self.session.capture_failure("report-history-not-loaded")
            raise NavigationError(
                f"The report history page did not render within "
                f"{self.settings.browser_timeout_ms} ms at {safe_url(self.page.url)} "
                "(config/selectors.json 'report_history_loaded_marker')."
            )
        self._wait_for_idle()

    def assert_report_exists(self) -> None:
        """Refuse an ID Amazon does not know, or one showing another report."""
        assert self.report is not None
        if is_present(self.page, "report_not_found_marker", timeout_ms=1_500):
            self.session.capture_failure("report-not-found")
            raise ReportIdError(
                f"Amazon does not recognise report {self.report.report_id} "
                f"(source: {self.report.source}). Check the configured report ID."
            )
        expected = self.report.expected_name
        if expected:
            try:
                body = self.page.inner_text("body", timeout=5_000)
            except Exception:
                body = ""
            if expected.lower() not in body.lower():
                self.session.capture_failure("report-name-mismatch")
                raise ReportIdError(
                    f"Report {self.report.report_id} page does not show the expected name "
                    f"{expected!r}. Refusing to run a report that may cover another date."
                )
            logger.info("Report page shows the expected name %r", expected)

    def _wait_for_idle(self) -> None:
        budget = min(self.settings.browser_timeout_ms, 15_000)
        for selector in candidates("loading_indicator"):
            try:
                self.page.locator(selector).first.wait_for(state="hidden", timeout=budget)
            except Exception:
                continue
        try:
            self.page.wait_for_load_state("networkidle", timeout=min(budget, 10_000))
        except PlaywrightTimeoutError:
            logger.debug("networkidle not reached; continuing")

    # -- read-only inspection -----------------------------------------------

    #: Selector keys used on the history page, checked by :meth:`inspect`.
    INSPECTED_KEYS = (
        "report_error_marker",
        "report_actions_menu",
        "report_history_loaded_marker",
        "report_not_found_marker",
        "run_report_button",
        "run_report_confirm_button",
        "history_row",
        "loading_indicator",
    )

    def inspect(self, report: ResolvedReport, *, open_actions_menu: bool = False) -> dict[str, object]:
        """Describe the page and test every configured selector.

        Clicks nothing, unless ``open_actions_menu``: then the Actions dropdown
        is opened, its entries are listed, and it is closed with Escape --
        no entry is ever chosen.

        Tolerates broken selectors on purpose (that is what it is for): it
        does not wait on 'report_history_loaded_marker', only for the page to
        settle.  Link targets are recorded without their query strings, so no
        signed download URL is written to disk.
        """
        self.report = report
        self._goto()
        if self.is_showing_sign_in():
            self.session.capture_failure("inspect-sign-in-required")
            raise AuthenticationError(
                "The saved session is not signed in. Run 'python run.py --login' first."
            )
        self.assert_authenticated()
        try:
            self.page.wait_for_load_state("networkidle", timeout=min(self.settings.browser_timeout_ms, 30_000))
        except PlaywrightTimeoutError:
            logger.info("Page kept polling the network; inspecting anyway")
        self.page.wait_for_timeout(3_000)

        def describe(locator: Locator, limit: int) -> list[str]:
            texts = []
            for i in range(min(locator.count(), limit)):
                try:
                    item = locator.nth(i)
                    if not item.is_visible():
                        continue
                    label = item.get_attribute("aria-label") or ""
                    text = " ".join(item.inner_text(timeout=1_000).split())[:120]
                    texts.append(f"{text} [aria-label={label}]" if label else text)
                except Exception:
                    continue
            return texts

        selectors: dict[str, list[dict[str, object]]] = {}
        for key in self.INSPECTED_KEYS:
            results = []
            for selector in candidates(key):
                try:
                    locator = self.page.locator(selector)
                    count = locator.count()
                    visible = count > 0 and locator.first.is_visible()
                except Exception as exc:
                    count, visible = -1, f"error: {type(exc).__name__}"
                results.append({"selector": selector, "matches": count, "first_visible": visible})
            selectors[key] = results

        rows = self._rows()
        top_row_download: list[dict[str, object]] = []
        if rows is not None:
            top = rows.nth(0)
            for selector in candidates("history_row_download"):
                try:
                    locator = top.locator(selector)
                    top_row_download.append(
                        {"selector": selector, "matches": locator.count(),
                         "first_visible": locator.count() > 0 and locator.first.is_visible()}
                    )
                except Exception as exc:
                    top_row_download.append({"selector": selector, "error": type(exc).__name__})
        selectors["history_row_download (inside newest row)"] = top_row_download

        links = []
        anchors = self.page.locator("a")
        for i in range(min(anchors.count(), 80)):
            try:
                a = anchors.nth(i)
                if a.is_visible():
                    links.append(
                        f"{' '.join(a.inner_text(timeout=1_000).split())[:80]} -> "
                        f"{safe_url(a.get_attribute('href') or '')}"
                    )
            except Exception:
                continue

        menu_items: list[str] | None = None
        selectors_after_menu: list[dict[str, object]] = []
        if open_actions_menu:
            menu_items, selectors_after_menu = self._list_actions_menu()

        snapshot = self.snapshot()
        return {
            "url": safe_url(self.page.url),
            "title": self.page.title(),
            "report_id": report.report_id,
            "selectors": selectors,
            "buttons": describe(self.page.locator("button, [role='button']"), 120),
            "column_headers": describe(self.page.locator("th, [role='columnheader']"), 40),
            "history_rows_matched": snapshot.row_count,
            "first_rows": describe(rows, 5) if rows is not None else [],
            "links": links,
            "actions_menu_items": menu_items,
            "run_report_menu_item": selectors_after_menu if open_actions_menu else None,
            "screenshot": str(self.session.screenshot("inspect-report-history") or ""),
        }

    def _list_actions_menu(self) -> tuple[list[str], list[dict[str, object]]]:
        """Open Actions, record what it offers, close it.  Chooses nothing."""
        menu = find_first(self.page, "report_actions_menu", timeout_ms=5_000)
        if menu is None:
            return ["(no Actions menu found)"], []
        menu.click()
        self.page.wait_for_timeout(1_500)
        items: list[str] = []
        for selector in ("[role='menuitem']", "[role='option']", "[role='menu'] li", "[role='listbox'] li"):
            locator = self.page.locator(selector)
            for i in range(min(locator.count(), 30)):
                try:
                    item = locator.nth(i)
                    if item.is_visible():
                        text = " ".join(item.inner_text(timeout=1_000).split())[:80]
                        entry = f"{selector}: {text}"
                        if text and entry not in items:
                            items.append(entry)
                except Exception:
                    continue
        tested = []
        for selector in candidates("run_report_menu_item"):
            try:
                locator = self.page.locator(selector)
                tested.append({"selector": selector, "matches": locator.count(),
                               "first_visible": locator.count() > 0 and locator.first.is_visible()})
            except Exception as exc:
                tested.append({"selector": selector, "error": type(exc).__name__})
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(500)
        return items, tested

    # -- history table ------------------------------------------------------

    def _rows(self) -> Locator | None:
        for selector in candidates("history_row"):
            try:
                locator = self.page.locator(selector)
                if locator.count():
                    return locator
            except Exception:
                continue
        return None

    def snapshot(self) -> HistorySnapshot:
        rows = self._rows()
        if rows is None:
            return HistorySnapshot(row_count=0, top_row="")
        try:
            count = rows.count()
            top = rows.nth(0).inner_text(timeout=2_000) if count else ""
        except Exception:
            return HistorySnapshot(row_count=0, top_row="")
        return HistorySnapshot(row_count=count, top_row=" ".join(top.split())[:300])

    def _top_row_download(self) -> Locator | None:
        rows = self._rows()
        if rows is None:
            return None
        top = rows.nth(0)
        for selector in candidates("history_row_download"):
            try:
                control = top.locator(selector).first
                if control.is_visible():
                    return control
            except Exception:
                continue
        return None

    def probe(self, before: HistorySnapshot) -> RunState:
        now = self.snapshot()
        state = classify_run(
            before, now, has_download=self._top_row_download() is not None, started_after=self._clicked_at
        )
        logger.debug("Top row (%d rows): %s", now.row_count, now.top_row)
        return state

    # -- run and download ---------------------------------------------------

    def run_report(self) -> HistorySnapshot:
        """Click Run report; return the snapshot new runs are compared against.

        If the button is disabled because a run is already in progress, that
        run is waited on instead of starting a second one.
        """
        before = self.snapshot()
        logger.info("History before run: %d row(s); top row: %s", before.row_count, before.top_row)
        if before.row_count > 0 and self._top_row_download() is None and not _FAILED.search(before.top_row):
            # Seen live (2026-10-20): a scheduled run sits at "Processing" with
            # no Download yet.  Starting a second run would only queue behind
            # it; wait for this one instead.
            logger.info("Newest run is still in progress; waiting for it instead of starting another")
            return HistorySnapshot.nothing()
        button = find_first(self.page, "run_report_button", timeout_ms=5_000)
        if button is None:
            # The live page (2026-10-20) has no visible Run button, only an
            # "Actions" menu; the run entry is expected inside it.
            menu = find_first(self.page, "report_actions_menu", timeout_ms=5_000)
            if menu is not None:
                menu.click()
                button = find_first(
                    self.page, "run_report_menu_item", timeout_ms=self.settings.browser_timeout_ms
                )
                if button is None:
                    self.page.keyboard.press("Escape")
        if button is None:
            self.session.capture_failure("run-report-control-missing")
            raise NavigationError(
                "Could not find the Run report control, neither as a button nor inside the "
                "Actions menu (config/selectors.json 'run_report_button' / 'run_report_menu_item')."
            )
        if not button.is_enabled():
            logger.warning("Run report is disabled -- a run is in progress; waiting on it")
            return HistorySnapshot.nothing()

        self._clicked_at = now_in(self.settings.amazon_account_timezone)
        button.click()
        confirm = find_first(self.page, "run_report_confirm_button", timeout_ms=3_000)
        if confirm is not None:
            confirm.click()
            logger.info("Confirmed the run dialog")
        logger.info("Run report clicked")
        return before

    def wait_for_completion(self, before: HistorySnapshot) -> int:
        polls = poll_until_ready(
            lambda: self.probe(before),
            self.reload,
            timeout_seconds=self.settings.report_ready_timeout_seconds,
            interval_seconds=self.settings.report_poll_interval_seconds,
        )
        logger.info("Report run completed after %d poll(s)", polls)
        return polls

    def download(self) -> DownloadResult:
        """Click the newest row's download action and save the file.

        The file may arrive on this page or on a tab the link opens, so both are
        watched.  A tab that just *displays* the CSV is fetched with the same
        session as a fallback.
        """
        assert self.report is not None
        if self._clicked_at is not None:
            top = self.snapshot().top_row
            if is_older_than(top, self._clicked_at):
                raise ReportNotReadyError(
                    f"The newest history row is an older run, not the one started at "
                    f"{self._clicked_at:%b %d %I:%M %p %Z}: {top[:120]}"
                )
        control = self._top_row_download()
        if control is None:
            self.session.capture_failure("download-control-missing")
            raise DownloadError(
                "The finished run shows no download control "
                "(config/selectors.json 'history_row_download')."
            )

        captured: list[Download] = []
        popups: list[Page] = []

        def on_download(download: Download) -> None:
            captured.append(download)

        def on_page(page: Page) -> None:
            popups.append(page)
            page.on("download", on_download)

        context = self.session.context
        self.page.on("download", on_download)
        context.on("page", on_page)
        try:
            control.click()
            deadline = time.monotonic() + self.settings.download_timeout_ms / 1000
            popup_seen_at: float | None = None
            while not captured and time.monotonic() < deadline:
                self.page.wait_for_timeout(250)
                if popups and popup_seen_at is None:
                    popup_seen_at = time.monotonic()
                if popup_seen_at is not None and time.monotonic() - popup_seen_at > 15:
                    break  # a tab opened and nothing downloaded from it
        finally:
            self.page.remove_listener("download", on_download)
            context.remove_listener("page", on_page)

        if captured:
            return self._save_download(captured[0])
        if popups:
            return self._fetch_popup(popups[0])
        self.session.capture_failure("download-timeout")
        raise DownloadError(
            f"Clicked download, but no file arrived within {self.settings.download_timeout_ms} ms."
        )

    def _save_download(self, download: Download) -> DownloadResult:
        assert self.report is not None
        failure = download.failure()
        if failure:
            self.session.capture_failure("download-failed")
            raise DownloadError(f"Download failed: {failure}")
        suggested = download.suggested_filename or "search_term.csv"
        target = self.settings.download_dir / target_filename(suggested, self.report.report_id)
        try:
            download.save_as(str(target))
        except Exception as exc:
            raise DownloadError(f"Could not save download to {target}: {exc}") from exc
        logger.info("Download captured: %s (suggested name %s)", target, suggested)
        size = validate_download(target)
        return DownloadResult(path=target, size_bytes=size, suggested_filename=suggested)

    def _fetch_popup(self, popup: Page) -> DownloadResult:
        assert self.report is not None
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            url = popup.url
            response = self.session.context.request.get(url, timeout=self.settings.download_timeout_ms)
        except Exception as exc:
            raise DownloadError(f"Could not fetch the report from the opened tab: {exc}") from exc
        finally:
            try:
                popup.close()
            except Exception:
                pass
        if not response.ok:
            raise DownloadError(f"Report link returned HTTP {response.status}")
        name = safe_url(url).rsplit("/", 1)[-1].split("?", 1)[0] or "search_term.csv"
        target = self.settings.download_dir / target_filename(name, self.report.report_id)
        target.write_bytes(response.body())
        logger.info("Report fetched from opened tab: %s", target)
        size = validate_download(target)
        return DownloadResult(path=target, size_bytes=size, suggested_filename=name)
