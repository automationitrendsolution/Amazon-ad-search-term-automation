"""Real Chromium against a local mock of reporting/history.

The mock is NOT Amazon's markup -- it only exercises our flow: an older run
already offers a Download, Run report adds a new row at '-', and the new row
turns into a Download after two reloads.  The test proves the new run's file
is the one downloaded, and that inspect() clicks nothing.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import date
from pathlib import Path

import pytest

from app.browser import BrowserSession
from app.config import Settings
from app.report_history import HistorySnapshot, ReportHistoryPage
from app.report_saver import ResolvedReport

MOCK_PAGE = """<!doctype html>
<html><body>
<!-- Modelled on the live page seen 2026-10-20: Actions menu (Run/Copy/Delete),
     AG Grid history, status Processing/Completed, Download link when done. -->
<button>Close report</button>
<button id="actions" aria-haspopup="menu">Actions</button>
<ul role="menu" id="menu" hidden>
  <li role="menuitem" id="run">Run</li>
  <li role="menuitem" id="copy">Copy</li>
  <li role="menuitem" id="del">Delete</li>
</ul>
<div role="grid"><div class="ag-center-cols-container" id="rows"></div></div>
<script>
// Like Amazon: the page rewrites its own URL while booting.
if (!location.search.includes('ephemeralAdvertiser=true')) {
  location.replace(location.href + '&ephemeralAdvertiser=true');
}
const OLD = 'Date,Campaign name\\n"Oct 18, 2026",old\\n';
const NEW = 'Date,Campaign name\\n"Oct 19, 2026",new\\n';
const initial = window.INITIAL_RUNS || '[{"t":"Oct 19, 2026 2:00 AM PDT","ready":true,"csv":"old"}]';
let runs = JSON.parse(localStorage.getItem('runs') || initial);
// Only a real load counts as a poll, not the pre-redirect one.
if (location.search.includes('ephemeralAdvertiser=true'))
  runs.forEach(r => { if (!r.ready) { r.polls = (r.polls || 0) + 1; if (r.polls >= 2) r.ready = true; } });
localStorage.setItem('runs', JSON.stringify(runs));
function render() {
  const body = document.getElementById('rows');
  body.innerHTML = '';
  runs.forEach(r => {
    const row = document.createElement('div');
    row.className = 'ag-row';
    row.innerHTML = '<span>' + (r.ready ? 'Completed' : 'Processing') + '</span> <span>' + r.t +
      '</span> <span>Today</span> <span>-</span> <span class="act"></span>';
    const cell = row.querySelector('.act');
    if (r.ready) {
      const a = document.createElement('a');
      a.textContent = 'Download';
      a.download = 'Targeting_today.csv';
      a.href = URL.createObjectURL(new Blob([r.csv === 'old' ? OLD : NEW], {type: 'text/csv'}));
      cell.appendChild(a);
    } else { cell.textContent = '-'; }
    body.appendChild(row);
  });
}
document.getElementById('actions').onclick = () => { document.getElementById('menu').hidden = false; };
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('menu').hidden = true; });
// Like Amazon: a new run's "Date created" is now, in the account timezone.
function createdNow() {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone: 'America/Los_Angeles',
    month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
    timeZoneName: 'short'}).formatToParts(new Date()).map(x => [x.type, x.value]));
  return p.month + ' ' + p.day + ', ' + p.year + ' ' + p.hour + ':' + p.minute + ' ' + p.dayPeriod + ' ' + p.timeZoneName;
}
document.getElementById('run').onclick = () => {
  runs.unshift({t: createdNow(), ready: false, csv: 'new'});
  localStorage.setItem('runs', JSON.stringify(runs));
  document.getElementById('menu').hidden = true;
  render();
};
document.getElementById('del').onclick = () => localStorage.setItem('deleted', 'yes');
document.getElementById('copy').onclick = () => localStorage.setItem('copied', 'yes');
render();
</script>
</body></html>
"""


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")


@pytest.fixture
def mock_settings(settings: Settings, tmp_path: Path, monkeypatch) -> Settings:
    page = tmp_path / "history.html"
    page.write_text(MOCK_PAGE, encoding="utf-8")
    monkeypatch.setattr(
        Settings,
        "history_url",
        lambda self, report_id: f"{page.as_uri()}?reportId={report_id}&ephemeraladvertiser=true",
    )
    return dataclasses.replace(
        settings,
        report_poll_interval_seconds=1,
        report_ready_timeout_seconds=30,
        browser_timeout_ms=10_000,
        download_timeout_ms=10_000,
    )


REPORT = ResolvedReport("58d08f36-8c45-4875-ae6b-cdd285b59721", date(2026, 10, 19), "test")


def test_run_wait_and_download_the_new_run(mock_settings: Settings):
    with BrowserSession(mock_settings, headless=True) as session:
        history = ReportHistoryPage(session, mock_settings)
        history.open(REPORT)
        before = history.run_report()
        assert before.row_count == 1
        polls = history.wait_for_completion(before)
        result = history.download()
        session_url = [session.page.url]
        untouched = session.page.evaluate("[localStorage.getItem('deleted'), localStorage.getItem('copied')]")

    assert polls >= 3  # pending, pending, ready -- reloads happened
    assert "ephemeralAdvertiser=true" in session_url[0]  # the redirect was followed
    text = result.path.read_text(encoding="utf-8")
    assert "Oct 19, 2026" in text and "old" not in text  # never the older run's file
    assert result.path.parent == mock_settings.download_dir
    assert untouched == [None, None]  # Delete and Copy were never clicked


def test_inspect_clicks_nothing(mock_settings: Settings):
    with BrowserSession(mock_settings, headless=True) as session:
        history = ReportHistoryPage(session, mock_settings)
        details = history.inspect(REPORT)
        assert history.snapshot().row_count == 1  # no new run started

    menu = details["selectors"]["report_actions_menu"]
    assert any(r["first_visible"] is True for r in menu)
    downloads = details["selectors"]["history_row_download (inside newest row)"]
    assert any(r["first_visible"] is True for r in downloads)
    assert Path(details["screenshot"]).is_file()


ERROR_PAGE = """<!doctype html><html><body>
<button>Close report</button><button>Actions</button>
<h2>Sorry, something went wrong</h2><a href="#">Refresh page</a> to try again
</body></html>"""


def test_amazon_error_panel_is_a_retryable_failure(settings: Settings, tmp_path: Path, monkeypatch):
    import time as _time

    from app.exceptions import NavigationError

    page = tmp_path / "error.html"
    page.write_text(ERROR_PAGE, encoding="utf-8")
    monkeypatch.setattr(Settings, "history_url", lambda self, _id: page.as_uri())
    quick = dataclasses.replace(settings, browser_timeout_ms=20_000)
    started = _time.monotonic()
    with BrowserSession(quick, headless=True) as session:
        with pytest.raises(NavigationError, match="Sorry, something went wrong") as info:
            ReportHistoryPage(session, quick).open(REPORT)
    assert info.value.transient is True
    assert _time.monotonic() - started < 15  # recognised at once, not after the full timeout


PROCESSING_RUNS = (
    '[{"t":"Oct 20, 2026 2:00 AM PDT","ready":false,"csv":"new"},'
    '{"t":"Oct 19, 2026 2:00 AM PDT","ready":true,"csv":"old"}]'
)


def test_run_already_processing_is_waited_on_not_restarted(settings: Settings, tmp_path: Path, monkeypatch):
    """Live page 2026-10-20: a scheduled run was 'Processing'. Do not start another."""
    page = tmp_path / "processing.html"
    seed = f"<script>window.INITIAL_RUNS = {PROCESSING_RUNS!r};</script>"
    page.write_text(MOCK_PAGE.replace("<body>", "<body>" + seed, 1), encoding="utf-8")
    monkeypatch.setattr(
        Settings, "history_url",
        lambda self, report_id: f"{page.as_uri()}?reportId={report_id}&ephemeraladvertiser=true",
    )
    quick = dataclasses.replace(
        settings, report_poll_interval_seconds=1, report_ready_timeout_seconds=30,
        browser_timeout_ms=10_000, download_timeout_ms=10_000,
    )
    with BrowserSession(quick, headless=True) as session:
        history = ReportHistoryPage(session, quick)
        history.open(REPORT)
        before = history.run_report()
        assert before == HistorySnapshot.nothing()  # waited on the existing run
        history.wait_for_completion(before)
        result = history.download()
        runs = session.page.evaluate("JSON.parse(localStorage.getItem('runs')).length")
    assert runs == 2  # no extra run was started
    assert "Oct 19, 2026" in result.path.read_text(encoding="utf-8")  # the processing run's file
