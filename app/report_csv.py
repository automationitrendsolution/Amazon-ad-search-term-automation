"""Reading and cleaning helpers for the Search term report.

* the file is decoded with the first encoding that works, and Amazon's
  report-metadata lines before the header are skipped;
* every data row must have as many fields as the header;
* ``="123"`` IDs become integers digit by digit (17-digit IDs keep every digit);
* ``$1,204.55`` / ``1.7964%`` / ``-`` become numbers or NULL;
* the Date column is parsed with one format that fits every row;
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .exceptions import CSVValidationError, DataTransformationError
from .logging_config import get_logger

logger = get_logger("csv")

#: Date formats tried, in order, when CSV_DATE_FORMAT is not set.  Day-first
#: slashes are deliberately absent: Amazon US writes MM/DD/YYYY, and guessing
#: between the two would silently swap day and month.
DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d-%b-%Y",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
)

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_HEADER_SCAN_LINES = 20
_NULL_TOKENS = frozenset({"", "-", "--", "—", "–", "n/a", "na", "null", "none", "nan"})
_NON_NUMERIC = re.compile(r"[^0-9eE.+-]")
_PARENTHESISED = re.compile(r"^\((.*)\)$")
_DIGITS = re.compile(r"^\d+$")
_INT64_MAX = 2**63 - 1


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def read_text(path: Path) -> tuple[str, str]:
    for encoding in _ENCODINGS:
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    raise CSVValidationError(f"Could not decode {path} with any of {_ENCODINGS}")


def detect_header(text: str) -> tuple[int, str]:
    """Find the header row: Amazon may prefix report-metadata lines."""
    for index, line in enumerate(text.splitlines()[:_HEADER_SCAN_LINES]):
        lowered = line.lower()
        if "campaign name" in lowered and "date" in lowered:
            delimiter = "\t" if line.count("\t") > line.count(",") else ","
            return index, delimiter
    raise CSVValidationError(
        f"No header row with 'Date' and 'Campaign name' in the first {_HEADER_SCAN_LINES} "
        "lines -- this is not the Search term report."
    )


def check_field_counts(text: str, header_row: int, delimiter: str, path: Path) -> None:
    """Every data row must have exactly as many fields as the header.

    Without this, pandas silently turns a row with one field too many into an
    index and shifts every value one column to the left.
    """
    body = "\n".join(text.splitlines()[header_row:])
    reader = csv.reader(io.StringIO(body), delimiter=delimiter)
    header = next(reader, [])
    for number, row in enumerate(reader, start=header_row + 2):
        if row and any(cell.strip() for cell in row) and len(row) != len(header):
            raise CSVValidationError(
                f"{path.name} line {number} has {len(row)} fields but the header has "
                f"{len(header)}; the file is malformed (check for unquoted commas)."
            )


# ---------------------------------------------------------------------------
# cleaning helpers
# ---------------------------------------------------------------------------


def clean_campaign_ids(
    series: pd.Series, *, source: str, labels: pd.Series | None = None
) -> pd.Series:
    """``="123456"`` / ``"123456"`` / ``123456`` -> nullable Int64.

    Converted digit-by-digit, never through float, so 17-digit IDs keep every
    digit.

    An invalid ID stops the run instead of dropping the row: loading the rest
    would replace that date with an incomplete set of targets.

    Args:
        labels: Optional per-row context (campaign names) for the error log.

    Raises:
        DataTransformationError: A value is blank or not a whole number.
    """
    text = (
        series.astype(str)
        .str.strip()
        .str.replace('="', "", regex=False)
        .str.replace('"', "", regex=False)
        .str.lstrip("=")
        .str.strip()
    )
    bad = ~text.str.match(_DIGITS)
    if bool(bad.any()):
        positions = [i for i, flag in enumerate(bad.tolist()) if flag]
        for position in positions[:50]:
            context = f" (campaign {labels.iloc[position]!r})" if labels is not None else ""
            logger.error(
                "Invalid ID in %s, data row %d: %r%s",
                source,
                position + 1,
                series.iloc[position],
                context,
            )
        raise DataTransformationError(
            f"{len(positions)} ID value(s) in {source} could not be converted to "
            f"integers (data rows {', '.join(str(p + 1) for p in positions[:20])}"
            f"{' ...' if len(positions) > 20 else ''}); see the log for each value."
        )
    values = [int(v) for v in text]
    if any(v > _INT64_MAX for v in values):
        raise DataTransformationError(f"An ID in {source} exceeds the 64-bit range")
    return pd.Series(pd.array(values, dtype="Int64"), index=series.index, name=series.name)


def clean_numeric_value(value: object) -> float | None:
    """``$1,234.56`` / ``12.5%`` / ``(45.00)`` / ``-`` -> float or None."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        return number if np.isfinite(number) else None
    text = str(value).strip()
    if text.lower() in _NULL_TOKENS:
        return None
    negative = False
    match = _PARENTHESISED.match(text)
    if match:
        negative, text = True, match.group(1)
    cleaned = _NON_NUMERIC.sub("", text)
    if cleaned.startswith("-"):
        negative = True
    cleaned = cleaned.lstrip("+-")
    if not cleaned or cleaned in {".", "e", "E"}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not np.isfinite(number):
        return None
    return -number if negative else number


def numeric(series: pd.Series, column: str) -> pd.Series:
    as_text = series.astype(str).str.strip().str.lower()
    parsed = series.map(clean_numeric_value).astype("Float64")
    lost = int((~as_text.isin(_NULL_TOKENS) & parsed.isna()).sum())
    if lost:
        raise DataTransformationError(
            f"{lost} value(s) in {column} are not numbers, e.g. "
            f"{series[~as_text.isin(_NULL_TOKENS) & parsed.isna()].head(3).tolist()}"
        )
    return parsed


def text(series: pd.Series) -> pd.Series:
    stripped = series.astype("string").str.strip()
    return stripped.mask(stripped.str.lower().isin(_NULL_TOKENS), pd.NA)


def parse_report_dates(series: pd.Series, explicit_format: str | None = None) -> pd.Series:
    """Parse the Date column with one format that fits every row.

    Raises:
        DataTransformationError: Blank dates, or no single format fits all.
    """
    values = series.astype(str).str.strip()
    if bool((values == "").any()):
        raise DataTransformationError(f"{int((values == '').sum())} row(s) have no Date")
    formats = (explicit_format,) if explicit_format else DATE_FORMATS
    for fmt in formats:
        try:
            parsed = [datetime.strptime(value, fmt).date() for value in values]
        except ValueError:
            continue
        logger.info("Date column parsed with format %r", fmt)
        return pd.Series(parsed, index=series.index, name=series.name, dtype=object)
    raise DataTransformationError(
        f"Date values such as {values.head(3).tolist()} do not match "
        f"{'CSV_DATE_FORMAT ' + repr(explicit_format) if explicit_format else 'any known format'}"
        ". Set CSV_DATE_FORMAT to the report's date format."
    )
