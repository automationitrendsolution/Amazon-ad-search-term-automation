"""CSV -> table rows for searchtermreport."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from app.exceptions import CSVValidationError, DataTransformationError, DateMismatchError
from app.search_term_processor import TABLE_COLUMNS, read_search_term_report, transform_search_term_report

from .conftest import HEADER, csv_text, line

TODAY = date(2026, 11, 6)
REPORT_ID = "58d08f36-8c45-4875-ae6b-cdd285b59721"


def _transform(tmp_path: Path, rows: list[str], **kwargs):
    path = tmp_path / "search_term_Automation.csv"
    path.write_text(csv_text(rows), encoding="utf-8")
    return transform_search_term_report(
        read_search_term_report(path),
        report_id=REPORT_ID,
        loaded_at=datetime(2026, 11, 6, 7, 15, 30, 123),
        today=kwargs.pop("today", TODAY),
        max_age_days=kwargs.pop("max_age_days", 8),
        **kwargs,
    )


def test_live_row_is_converted(tmp_path: Path):
    report = _transform(tmp_path, [line(imp=655, clicks=2, cost="1.23450", sales="19.99", purchases=1, units=1,
                                        ctr="0.3053%")])
    row = report.frame.iloc[0]
    assert list(report.frame.columns) == list(TABLE_COLUMNS)
    assert row["report_date"] == TODAY
    assert row["hour"] == 2
    assert row["campaign_id"] == 141242384338773  # ="..." unwrapped, exact
    assert row["ad_group_id"] == 188245137651693
    assert row["target_id"] == 22559448179860
    assert row["campaign_country"] == "GB"
    assert row["impressions"] == 655 and row["clicks"] == 2
    assert row["total_cost"] == pytest.approx(1.2345)
    assert row["sales"] == pytest.approx(19.99)
    assert row["ctr"] == pytest.approx(0.3053)  # percent, as Amazon shows it
    assert row["target_type"] is pd.NA  # blank -> NULL
    assert row["country"] is pd.NA
    assert row["report_id"] == REPORT_ID
    assert row["loaded_at"] == datetime(2026, 11, 6, 7, 15, 30)
    assert report.slices == ((TODAY, "GB"),)


def test_blank_cpc_and_ctr_are_null_but_metrics_default_to_zero(tmp_path: Path):
    row = _transform(tmp_path, [line(cpc="", ctr="", clicks=0, cost="0.00000")]).frame.iloc[0]
    assert row["cpc"] is pd.NA and row["ctr"] is pd.NA
    assert row["total_cost"] == 0


def test_real_search_terms_that_look_like_nulls_are_kept(tmp_path: Path):
    rows = [line(term=t, hour=i) for i, t in enumerate(["none", "na", "-", "n/a"])]
    terms = _transform(tmp_path, rows).frame["search_term"].tolist()
    assert terms == ["none", "na", "-", "n/a"]


def test_targeting_expression_with_quotes(tmp_path: Path):
    row = _transform(tmp_path, [line(targeting='"asin=""B07GYYGJZ9"""', match="TARGETING_EXPRESSION")]).frame.iloc[0]
    assert row["targeting"] == 'asin="B07GYYGJZ9"'


def test_blank_target_id_and_no_portfolio(tmp_path: Path):
    row = _transform(tmp_path, [line(target="", portfolio="-1")]).frame.iloc[0]
    assert row["target_id"] is pd.NA
    assert row["portfolio_id"] == -1


def test_several_marketplaces_and_dates_become_slices(tmp_path: Path):
    rows = [
        line(country="US", day="Nov 05, 2026"),
        line(country="GB"),
        line(country="DE", hour=3),
        line(country="US", hour=4),
    ]
    report = _transform(tmp_path, rows)
    assert report.slices == ((date(2026, 11, 5), "US"), (TODAY, "DE"), (TODAY, "GB"), (TODAY, "US"))
    assert report.describe() == "2026-11-05 [US]; 2026-11-06 [DE, GB, US]"


def test_duplicate_rows_are_refused(tmp_path: Path):
    with pytest.raises(DataTransformationError, match="repeat"):
        _transform(tmp_path, [line(), line()])


def test_same_term_in_another_hour_is_not_a_duplicate(tmp_path: Path):
    assert _transform(tmp_path, [line(hour=1), line(hour=2)]).row_count == 2


def test_old_dates_are_refused(tmp_path: Path):
    with pytest.raises(DateMismatchError, match="2026-10-01"):
        _transform(tmp_path, [line(day="Oct 01, 2026")])


def test_last_week_fits_the_default_window(tmp_path: Path):
    assert _transform(tmp_path, [line(day="Oct 30, 2026")]).row_count == 1


def test_future_dates_beyond_tomorrow_are_refused(tmp_path: Path):
    _transform(tmp_path, [line(day="Nov 07, 2026")])  # an account east of LA
    with pytest.raises(DateMismatchError):
        _transform(tmp_path, [line(day="Nov 08, 2026")])


def test_bad_hour(tmp_path: Path):
    with pytest.raises(DataTransformationError, match="Hour"):
        _transform(tmp_path, [line(hour=24)])


def test_missing_column(tmp_path: Path):
    path = tmp_path / "r.csv"
    path.write_text(HEADER.replace(",CTR", "") + "\n", encoding="utf-8")
    with pytest.raises(CSVValidationError, match="CTR"):
        read_search_term_report(path)


def test_not_a_search_term_report(tmp_path: Path):
    path = tmp_path / "r.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(CSVValidationError, match="Search term"):
        read_search_term_report(path)


def test_header_only_file_has_no_rows(tmp_path: Path):
    report = _transform(tmp_path, [])
    assert report.row_count == 0 and report.slices == ()


def test_amazon_formatted_numbers_still_parse(tmp_path: Path):
    row = _transform(tmp_path, [line(sales='"$1,204.55"', cost="-")]).frame.iloc[0]
    assert row["sales"] == pytest.approx(1204.55)
    assert row["total_cost"] == 0


def test_no_window_for_backfills(tmp_path: Path):
    assert _transform(tmp_path, [line(day="Jan 01, 2026")], today=None).row_count == 1
