"""Which saved report(s) a run fetches, and the latest copy kept on disk.

Every run fetches the Today report (``search_term_Automation``, report period
Today).  When a second saved report with period Yesterday is configured, it
is also fetched during the first YESTERDAY_FINALIZE_HOURS hours of the day in
AMAZON_ACCOUNT_TIMEZONE (00:00-02:00 Los Angeles = 12:30-14:30 IST in US
summer time), when every marketplace's previous day has ended -- so that day
gets its last hour and late attributions.

The latest file is published as ``data/reports/search_term_<kind>.csv`` only
after it has been validated and committed, so a failed download never
overwrites the last good copy.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .database import WriteResult
from .logging_config import get_logger
from .timeutils import now_in

logger = get_logger("saver")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ResolvedReport:
    """One saved Amazon report definition."""

    report_id: str
    #: "Today" in the calendar timezone when the run started (for the logs).
    report_date: date
    #: Where the ID came from: "relative-today", "relative-yesterday", "cli".
    source: str
    #: Text the report page must contain, when configured.
    expected_name: str | None = None

    @property
    def kind(self) -> str:
        return self.source.split("-", 1)[-1]


@dataclass
class SavedReport:
    kind: str
    report_id: str
    saved_to: Path | None = None
    rows: int = 0
    #: "2026-11-06 [GB, US]; ..." -- the dates/marketplaces the file held.
    coverage: str | None = None
    error: str | None = None
    write: WriteResult | None = None


def calendar_today(settings: Settings, *, reference: datetime | None = None) -> date:
    return now_in(settings.amazon_account_timezone, reference=reference).date()


def reports_for_now(settings: Settings, *, reference: datetime | None = None) -> list[ResolvedReport]:
    """Every configured report: Today, then Yesterday when set."""
    today = calendar_today(settings, reference=reference)
    reports = []
    if settings.report_id_today:
        reports.append(ResolvedReport(settings.report_id_today, today, "relative-today", settings.report_name_today))
    if settings.report_id_yesterday:
        reports.append(
            ResolvedReport(
                settings.report_id_yesterday,
                today - timedelta(days=1),
                "relative-yesterday",
                settings.report_name_yesterday,
            )
        )
    return reports


def yesterday_due(settings: Settings, *, reference: datetime | None = None) -> bool:
    clock = now_in(settings.amazon_account_timezone, reference=reference)
    return clock.hour < settings.yesterday_finalize_hours


def reports_for_run(settings: Settings, *, reference: datetime | None = None) -> list[ResolvedReport]:
    """Today every run; Yesterday too while the day is younger than YESTERDAY_FINALIZE_HOURS."""
    due = yesterday_due(settings, reference=reference)
    return [r for r in reports_for_now(settings, reference=reference) if r.kind == "today" or due]


def published_name(kind: str) -> str:
    return f"amazon_ad_search_term_automation_{_SAFE_NAME.sub('_', kind)}.csv"


def publish(source: Path, target: Path) -> Path:
    """Copy ``source`` over ``target`` atomically (readers never see half a file)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)
    return target
