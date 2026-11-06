"""Stage 4: completion detection and download validation (no browser)."""

from __future__ import annotations

import gzip
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.downloader import target_filename, validate_download
from app.exceptions import CSVValidationError, ReportNotReadyError
from app.report_history import HistorySnapshot, RunState, classify_run, poll_until_ready, row_created_at

BEFORE = HistorySnapshot(row_count=3, top_row="Oct 19, 2026 10:00 Download")


def test_old_completed_row_is_not_our_run():
    # Same table as before the click: its Download link belongs to an old run.
    assert classify_run(BEFORE, BEFORE, has_download=True) is RunState.NOT_STARTED


def test_new_row_pending_while_status_is_dash():
    now = HistorySnapshot(4, "Oct 20, 2026 13:00 -")
    assert classify_run(BEFORE, now, has_download=False) is RunState.PENDING


def test_new_row_ready_once_download_appears():
    now = HistorySnapshot(4, "Oct 20, 2026 13:00 Download")
    assert classify_run(BEFORE, now, has_download=True) is RunState.READY


def test_new_row_failed():
    now = HistorySnapshot(4, "Oct 20, 2026 13:00 Failed")
    assert classify_run(BEFORE, now, has_download=False) is RunState.FAILED


def test_paginated_table_detects_new_top_row():
    now = HistorySnapshot(3, "Oct 20, 2026 13:00 -")
    assert classify_run(BEFORE, now, has_download=False) is RunState.PENDING


def test_in_progress_run_counts_when_button_disabled():
    now = HistorySnapshot(3, "Oct 19, 2026 10:00 -")
    assert classify_run(HistorySnapshot.nothing(), now, has_download=False) is RunState.PENDING


# Live failure, 2026-10-23 01:01 server time: right after Run was clicked the
# table re-rendered with an Oct 20 run on top; its Download was taken as ours.
PDT = ZoneInfo("America/Los_Angeles")
CLICKED = datetime(2026, 10, 22, 22, 1, 37, tzinfo=PDT)
LIVE_BEFORE = HistorySnapshot(26, "Completed Oct 22, 2026 9:01 PM PDT Today — Download")


def test_older_run_shuffled_to_the_top_is_not_ours():
    shuffled = HistorySnapshot(26, "Completed Oct 20, 2026 11:30 PM PDT Today — Download")
    assert classify_run(LIVE_BEFORE, shuffled, has_download=True, started_after=CLICKED) is RunState.NOT_STARTED


def test_run_created_at_the_click_is_ours():
    pending = HistorySnapshot(27, "Processing Oct 22, 2026 10:01 PM PDT Today — —")
    ready = HistorySnapshot(27, "Completed Oct 22, 2026 10:01 PM PDT Today — Download")
    assert classify_run(LIVE_BEFORE, pending, has_download=False, started_after=CLICKED) is RunState.PENDING
    assert classify_run(LIVE_BEFORE, ready, has_download=True, started_after=CLICKED) is RunState.READY


def test_run_started_a_minute_earlier_by_another_job_is_accepted():
    # Same report, same period: e.g. the campaign job clicked Run a minute before us.
    other = HistorySnapshot(27, "Completed Oct 22, 2026 10:00 PM PDT Today — Download")
    assert classify_run(LIVE_BEFORE, other, has_download=True, started_after=CLICKED) is RunState.READY


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Completed Oct 22, 2026 9:01 PM PDT Today", datetime(2026, 10, 22, 21, 1, tzinfo=PDT)),
        ("Processing Oct 3, 2026 12:05 AM PDT Yesterday", datetime(2026, 10, 3, 0, 5, tzinfo=PDT)),
        ("Oct 20, 2026 13:00 Download", None),  # no AM/PM: unreadable, old rule applies
    ],
)
def test_row_created_at(text, expected):
    assert row_created_at(text, PDT) == expected


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_poll_reloads_until_ready():
    states = iter([RunState.NOT_STARTED, RunState.PENDING, RunState.PENDING, RunState.READY])
    reloads = []
    clock = FakeClock()
    polls = poll_until_ready(
        lambda: next(states), lambda: reloads.append(1),
        timeout_seconds=300, interval_seconds=30, clock=clock, sleep=clock.sleep,
    )
    assert polls == 4 and len(reloads) == 3


def test_poll_times_out_while_still_dash():
    clock = FakeClock()
    with pytest.raises(ReportNotReadyError, match="still pending"):
        poll_until_ready(
            lambda: RunState.PENDING, lambda: None,
            timeout_seconds=120, interval_seconds=30, clock=clock, sleep=clock.sleep,
        )
    assert clock.now <= 120


def test_poll_stops_on_failed_run():
    clock = FakeClock()
    with pytest.raises(ReportNotReadyError, match="failed"):
        poll_until_ready(lambda: RunState.FAILED, lambda: None,
                         timeout_seconds=120, interval_seconds=30, clock=clock, sleep=clock.sleep)


def test_report_not_ready_is_retryable():
    assert ReportNotReadyError("x").transient is True


def test_validate_download_accepts_csv(tmp_path: Path):
    path = tmp_path / "r.csv"
    path.write_text("Date,Campaign name,Campaign ID\nOct 19, 2026,A,1\n", encoding="utf-8")
    assert validate_download(path) == path.stat().st_size


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "too small"),
        (b"<!DOCTYPE html><html><body>Sign in</body></html>" * 2, "HTML"),
        (b"PK\x03\x04" + b"\x00" * 64, "Excel"),
        (b"just some text without delimiters at all, sorry"[:0] + b"x" * 64, "no delimited header"),
    ],
)
def test_validate_download_rejects(tmp_path: Path, content: bytes, message: str):
    path = tmp_path / "r.csv"
    path.write_bytes(content)
    with pytest.raises(CSVValidationError, match=message):
        validate_download(path)


def test_validate_download_missing(tmp_path: Path):
    with pytest.raises(CSVValidationError, match="does not exist"):
        validate_download(tmp_path / "absent.csv")


def test_gzip_report_is_decompressed(tmp_path: Path):
    path = tmp_path / "r.csv"
    body = b"Date,Campaign name,Campaign ID\nOct 19, 2026,A,1\n"
    path.write_bytes(gzip.compress(body))
    validate_download(path)
    assert path.read_bytes() == body


def test_target_filename_is_owned_and_csv():
    name = target_filename("Sponsored Products Targeting report.csv.gz", "58d08f36-8c45")
    assert name.endswith("_58d08f36_Sponsored_Products_Targeting_report.csv")


# -- Amazon's client-side redirect during navigation -------------------------

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

from app.exceptions import NavigationError  # noqa: E402
from app.report_history import ReportHistoryPage  # noqa: E402

INTERRUPTED = (
    'Page.goto: Navigation to "https://advertising.amazon.com/reporting/history?reportId=x'
    '&ephemeraladvertiser=true" is interrupted by another navigation to '
    '"https://advertising.amazon.com/reporting/history?reportId=x&ephemeraladvertiser=true'
    '&ephemeralAdvertiser=true"'
)


class FakePage:
    def __init__(self, error: Exception | None):
        self.error = error
        self.url = "https://advertising.amazon.com/reporting/history?reportId=x&ephemeralAdvertiser=true"
        self.waited = []

    def goto(self, url, wait_until=None):
        if self.error:
            raise self.error

    def wait_for_load_state(self, state, timeout=None):
        self.waited.append(state)


def _history(settings, page):
    session = type("S", (), {"page": page})()
    return ReportHistoryPage(session, settings)


def test_amazon_redirect_during_goto_is_followed(settings):
    page = FakePage(PlaywrightError(INTERRUPTED))
    history = _history(settings, page)
    history._navigate(lambda: page.goto("u"), "loading the report history page")
    assert page.waited == ["domcontentloaded"]
    assert history.is_on_history_page()


def test_other_navigation_errors_still_fail(settings):
    page = FakePage(PlaywrightError("net::ERR_NAME_NOT_RESOLVED"))
    with pytest.raises(NavigationError, match="ERR_NAME_NOT_RESOLVED"):
        _history(settings, page)._navigate(lambda: page.goto("u"), "loading")
