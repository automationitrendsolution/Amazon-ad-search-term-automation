"""Search term CSV -> rows for ``gen_ai_db.searchtermreport``.

The saved ``search_term_Automation`` report (Sponsored Products / Brands,
Hourly, CSV, "All accounts") has 32 columns, one row per
date / hour / campaign / ad group / target / search term.  Every column is
kept, renamed to snake_case:

* ``="123"`` IDs become exact 64-bit integers (never through float);
* money, counts and ``12.5%`` CTR become numbers (CTR stays a percentage);
* blank text becomes NULL;
* ``report_date`` is the parsed Date, ``report_id`` / ``loaded_at`` record
  which saved report and which run produced the row;
* ``row_key`` is the SHA-256 of the row's natural key (see :func:`row_key`),
  which upsert / insert loads match existing rows on.

The report covers several marketplaces (US, CA, MX, GB, DE, FR, IT) and each
account reports in its own timezone, so one file can hold more than one date.
A replace load therefore replaces each (``report_date``, ``campaign_country``)
pair the file contains; upsert / insert loads match rows on ``row_key`` -- see
:mod:`app.database`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import ROW_KEY_COLUMN
from .exceptions import CSVValidationError, DataTransformationError, DateMismatchError
from .logging_config import get_logger
from .report_csv import check_field_counts, detect_header, numeric, parse_report_dates, read_text

logger = get_logger("search_term")

#: Amazon column -> (database column, kind).  Order = Amazon's export order.
COLUMN_MAP: dict[str, tuple[str, str]] = {
    "Date": ("report_date", "date"),
    "Target ID": ("target_id", "id?"),
    "Target bid": ("target_bid", "number"),
    "Target type": ("target_type", "text"),
    "Target status": ("target_status", "text"),
    "Targeting": ("targeting", "text"),
    "Targeting match type": ("targeting_match_type", "text"),
    "Campaign ID": ("campaign_id", "id"),
    "Campaign name": ("campaign_name", "text!"),
    "Campaign budget amount": ("campaign_budget_amount", "number"),
    "Campaign delivery status": ("campaign_delivery_status", "text"),
    "Campaign country": ("campaign_country", "text!"),
    "Budget currency": ("budget_currency", "text"),
    "Ad group ID": ("ad_group_id", "id?"),
    "Ad group name": ("ad_group_name", "text"),
    "Portfolio ID": ("portfolio_id", "id?"),
    "Portfolio name": ("portfolio_name", "text"),
    "Country": ("country", "text"),
    "Country code": ("country_code", "text"),
    "Search term": ("search_term", "text"),
    "Hour": ("hour", "hour"),
    "Impressions": ("impressions", "count"),
    "Clicks": ("clicks", "count"),
    "Total cost": ("total_cost", "metric"),
    "Purchases": ("purchases", "count"),
    "Purchases (multi-touch)": ("purchases_multi_touch", "metric"),
    "Sales": ("sales", "metric"),
    "Sales (multi-touch)": ("sales_multi_touch", "metric"),
    "Units sold": ("units_sold", "count"),
    "Units sold (multi-touch)": ("units_sold_multi_touch", "metric"),
    "CPC": ("cpc", "number"),
    "CTR": ("ctr", "number"),
}

#: Every column the loader writes, in table order.
TABLE_COLUMNS: tuple[str, ...] = (
    *(db for db, _ in COLUMN_MAP.values()),
    "report_id",
    "loaded_at",
    ROW_KEY_COLUMN,
)
DATE_COLUMN = "report_date"
SLICE_COLUMN = "campaign_country"

#: One row per date / hour / marketplace / campaign / ad group / target / search term.
NATURAL_KEY: tuple[str, ...] = (
    "report_date",
    "hour",
    "campaign_country",
    "campaign_id",
    "ad_group_id",
    "target_id",
    "search_term",
)



def row_key(values: tuple[object, ...]) -> str:
    """SHA-256 hex of one row's NATURAL_KEY values.

    Byte-for-byte the same as the MySQL backfill in
    ``sql/migrate_add_row_key.sql``::

        SHA2(CONCAT(report_date, '|', hour, '|', campaign_country, '|', campaign_id, '|',
                    COALESCE(ad_group_id, ''), '|', COALESCE(target_id, ''), '|',
                    COALESCE(search_term, '')), 256)

    A NULL becomes '' (blank text is always stored as NULL, never '', so the
    two cannot collide).  Unlike a UNIQUE index on the raw columns, the hash
    treats NULL target IDs as equal and compares search terms exactly (the
    table's collation is case- and accent-insensitive).
    """
    parts = []
    for value in values:
        if value is None or value is pd.NA:
            parts.append("")
        elif isinstance(value, date):
            parts.append(value.isoformat())
        else:
            parts.append(str(value))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


_ID = re.compile(r"^-?\d+$")
_INT64_MAX = 2**63 - 1


@dataclass(frozen=True)
class SearchTermReport:
    frame: pd.DataFrame
    #: (report_date, campaign_country) pairs the file contains.
    slices: tuple[tuple[date, str], ...]

    @property
    def row_count(self) -> int:
        return len(self.frame)

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(sorted({d for d, _ in self.slices}))

    def describe(self) -> str:
        if not self.slices:
            return "no rows"
        per_date: dict[date, list[str]] = {}
        for day, country in self.slices:
            per_date.setdefault(day, []).append(country)
        return "; ".join(f"{d.isoformat()} [{', '.join(sorted(c))}]" for d, c in sorted(per_date.items()))


def read_search_term_report(path: Path) -> pd.DataFrame:
    """The CSV as strings, header located, every row's field count checked.

    Raises:
        CSVValidationError: Unreadable, no header, malformed rows or missing columns.
    """
    content, encoding = read_text(path)
    header_row, delimiter = detect_header(content)
    check_field_counts(content, header_row, delimiter, path)
    try:
        frame = pd.read_csv(
            path,
            encoding=encoding,
            sep=delimiter,
            skiprows=header_row,
            dtype=str,
            keep_default_na=False,
            engine="c",
            low_memory=False,
        )
    except Exception as exc:
        raise CSVValidationError(f"Could not read {path.name}: {exc}") from exc
    frame.columns = [str(c).strip().lstrip("﻿") for c in frame.columns]
    missing = [c for c in COLUMN_MAP if c not in frame.columns]
    if missing:
        raise CSVValidationError(
            f"{path.name} is missing column(s) {', '.join(missing)}. Check the saved report's "
            "columns in Amazon Ads (search_term_Automation > Report settings)."
        )
    extra = [c for c in frame.columns if c not in COLUMN_MAP]
    if extra:
        logger.warning("Ignoring column(s) not in the table: %s", ", ".join(extra))
    blank = frame.apply(lambda col: col.str.strip() == "").all(axis=1)
    if bool(blank.any()):
        frame = frame.loc[~blank]
    logger.info("Read %s: %d data row(s)", path.name, len(frame))
    return frame.reset_index(drop=True)


def _ids(series: pd.Series, column: str, *, required: bool) -> pd.Series:
    """``="123"`` / ``123`` / ``-1`` / blank -> nullable Int64, digit by digit."""
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace('="', "", regex=False)
        .str.replace('"', "", regex=False)
        .str.lstrip("=")
        .str.strip()
    )
    blank = cleaned == ""
    if required and bool(blank.any()):
        raise DataTransformationError(f"{int(blank.sum())} row(s) have no {column}")
    bad = ~blank & ~cleaned.str.match(_ID)
    if bool(bad.any()):
        raise DataTransformationError(
            f"{int(bad.sum())} {column} value(s) are not whole numbers, e.g. "
            f"{series[bad].head(3).tolist()}"
        )
    values = [None if b else int(v) for v, b in zip(cleaned, blank)]
    if any(v is not None and abs(v) > _INT64_MAX for v in values):
        raise DataTransformationError(f"A {column} value exceeds the 64-bit range")
    return pd.Series(pd.array(values, dtype="Int64"), index=series.index)


def _text(series: pd.Series) -> pd.Series:
    """Strip; only a blank cell is NULL.  "none", "na" or "-" are real search terms."""
    stripped = series.astype("string").str.strip()
    return stripped.mask(stripped == "", pd.NA)


_PLAIN_NUMBER = re.compile(r"^-?(\d+\.?\d*|\.\d+)$")


def _numbers(series: pd.Series, column: str) -> pd.Series:
    """Fast path for Amazon's plain ``12.34000`` / ``0.3053%`` cells; anything
    else (currency signs, thousands separators, ``-``) goes through the
    general :func:`app.report_csv.numeric` cleaner."""
    cleaned = series.astype(str).str.strip().str.removesuffix("%")
    blank = cleaned == ""
    if bool(cleaned[~blank].str.match(_PLAIN_NUMBER).all()):
        return pd.to_numeric(cleaned.mask(blank, None), errors="raise").astype("Float64")
    return numeric(series, column)


def _hours(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.strip()
    bad = ~cleaned.str.fullmatch(r"\d{1,2}")
    if not bad.any():
        values = cleaned.astype(int)
        bad = (values < 0) | (values > 23)
        if not bad.any():
            return values.astype("Int64")
    raise DataTransformationError(
        f"{int(bad.sum())} Hour value(s) are not 0-23, e.g. {series[bad].head(3).tolist()}"
    )


def transform_search_term_report(
    raw: pd.DataFrame,
    *,
    report_id: str,
    loaded_at: datetime,
    today: date | None,
    max_age_days: int,
    csv_date_format: str | None = None,
) -> SearchTermReport:
    """Validate and convert the raw CSV into table rows.

    Args:
        today: "Today" in the calendar timezone; no row may be dated after
            today + 1 (an account east of it) or before today - max_age_days.
            None skips the check (``--process-file`` backfills).

    Raises:
        DataTransformationError: A value cannot be converted, or a row repeats.
        DateMismatchError: A row's date is outside the allowed window.
    """
    if raw.empty:
        return SearchTermReport(pd.DataFrame(columns=list(TABLE_COLUMNS)), ())

    out = pd.DataFrame(index=raw.index)
    for source, (target, kind) in COLUMN_MAP.items():
        series = raw[source]
        if kind == "date":
            out[target] = parse_report_dates(series, csv_date_format)
        elif kind in ("id", "id?"):
            out[target] = _ids(series, source, required=kind == "id")
        elif kind == "hour":
            out[target] = _hours(series)
        elif kind == "count":
            values = _numbers(series, source).fillna(0)
            if bool((values != values.round()).any()):
                raise DataTransformationError(f"{source} has fractional values")
            out[target] = values.round().astype("Int64")
        elif kind == "metric":
            out[target] = _numbers(series, source).fillna(0.0)
        elif kind == "number":
            out[target] = _numbers(series, source)
        else:  # text / text!
            values = _text(series)
            if kind == "text!" and bool(values.isna().any()):
                raise DataTransformationError(f"{int(values.isna().sum())} row(s) have no {source}")
            out[target] = values
    out["campaign_country"] = out["campaign_country"].str.upper()
    out["report_id"] = report_id
    out["loaded_at"] = loaded_at.replace(tzinfo=None, microsecond=0)

    dates = set(out[DATE_COLUMN])
    if today is not None:
        earliest, latest = today - timedelta(days=max_age_days), today + timedelta(days=1)
        outside = sorted(d for d in dates if d < earliest or d > latest)
        if outside:
            raise DateMismatchError(
                f"The report contains date(s) {', '.join(d.isoformat() for d in outside[:5])} outside "
                f"{earliest.isoformat()}..{latest.isoformat()}. Check the saved report's period "
                "(it should be Today), or raise MAX_REPORT_AGE_DAYS for a longer period."
            )

    duplicated = out.duplicated(list(NATURAL_KEY), keep=False)
    if bool(duplicated.any()):
        sample = out.loc[duplicated, list(NATURAL_KEY)].head(3).to_dict(orient="records")
        raise DataTransformationError(
            f"{int(duplicated.sum())} row(s) repeat the same date/hour/campaign/ad group/target/"
            f"search term, e.g. {sample}; the file is not a clean Search term export."
        )

    out[ROW_KEY_COLUMN] = [
        row_key(values) for values in zip(*(out[c].tolist() for c in NATURAL_KEY))
    ]

    slices = tuple(
        sorted({(d, c) for d, c in zip(out[DATE_COLUMN], out[SLICE_COLUMN])})
    )
    frame = out.loc[:, list(TABLE_COLUMNS)].reset_index(drop=True)
    logger.info("Prepared %d row(s): %s", len(frame), SearchTermReport(frame, slices).describe())
    return SearchTermReport(frame, slices)

