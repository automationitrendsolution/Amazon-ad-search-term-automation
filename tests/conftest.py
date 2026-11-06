"""Shared fixtures.  Nothing here touches Amazon or a real MySQL server.

Database tests run against in-memory SQLite with a table shaped like
``gen_ai_db.searchtermreport`` (sql/create_searchtermreport.sql),
so transactions and rollbacks are exercised for real.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import ROW_KEY_COLUMN, TARGET_TABLE, Settings, load_settings
from app.database import SearchTermDatabase
from app.search_term_processor import COLUMN_MAP, TABLE_COLUMNS

REPORT_ID_TODAY = "58d08f36-8c45-4875-ae6b-cdd285b59721"
REPORT_ID_YESTERDAY = "15a08763-73ca-4172-8029-521d5ed7d090"

BASE_ENV = {
    "DB_HOST": "localhost",
    "DB_NAME": "gen_ai_db",
    "DB_USER": "tester",
    "DB_PASSWORD": "s3cret",
    "AMAZON_ACCOUNT_TIMEZONE": "America/Los_Angeles",
    "REPORT_ID_TODAY": REPORT_ID_TODAY,
}

#: The live search_term_Automation export's 32 columns, in Amazon's order.
HEADER = ",".join(COLUMN_MAP)


def line(day: str = "Nov 06, 2026", hour: int = 2, *, target: str = "22559448179860",
         campaign: str = "141242384338773", ad_group: str = "188245137651693", country: str = "GB",
         term: str = "(Other)", targeting: str = "disposable rain poncho", match: str = "PHRASE",
         campaign_name: str = "rainponcho_Phrase", imp: int = 10, clicks: int = 1, cost: str = "0.45000",
         purchases: int = 0, sales: str = "0.00", units: int = 0, cpc: str = "0.45000", ctr: str = "10.0000%",
         portfolio: str = "62729998740198") -> str:
    """One data row, formatted exactly as Amazon writes it (2026-11-06 export)."""
    return (
        f'"{day}",{target},0.92,,ENABLED,{targeting},{match},"=""{campaign}""",{campaign_name},3,ENABLED,'
        f'{country},GBP,"=""{ad_group}""",Ad Group - 25/08/2026 12:07:09.885,{portfolio},RainCover,,,'
        f"{term},{hour},{imp},{clicks},{cost},{purchases},0.00000,{sales},0.00000,{units},0.00000,{cpc},{ctr}"
    )


def csv_text(rows: list[str]) -> str:
    return "﻿" + "\n".join([HEADER, *rows]) + "\n"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ignore the developer's .env and exported variables."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *_a, **_k: False)
    for key in list(os.environ):
        if key.startswith(("AMAZON_", "DB_", "REPORT_", "CSV_", "DOWNLOAD_", "REPORTS_", "MAX_REPORT",
                           "ARCHIVE_", "FAILED_", "LOG_", "SCREENSHOT_", "SESSION_", "LOCK_", "YESTERDAY_",
                           "HEADLESS", "BROWSER_", "MAX_RETRIES", "RETRY_", "FILE_RETENTION")):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(BASE_ENV)


@pytest.fixture
def settings(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, name in (
        ("DOWNLOAD_DIR", "downloads"),
        ("REPORTS_DIR", "reports"),
        ("ARCHIVE_DIR", "archive"),
        ("FAILED_DIR", "failed"),
        ("SCREENSHOT_DIR", "screenshots"),
        ("LOG_DIR", "logs"),
        ("SESSION_DIR", "session"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "run.lock"))
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    loaded = load_settings()
    loaded.ensure_directories()
    return loaded


_TYPES = {
    "date": Date, "id": BigInteger, "id?": BigInteger, "hour": Integer, "count": Integer,
    "metric": Numeric(14, 5), "number": Numeric(14, 5), "text": String(512), "text!": String(512),
}


def make_table(metadata: MetaData, *, with_row_key: bool = True) -> Table:
    """SQLite stand-in for sql/create_searchtermreport.sql.

    ``with_row_key=False`` is a table created before row_key existed.
    """
    columns = [Column("id", Integer, primary_key=True, autoincrement=True)]
    for db, kind in COLUMN_MAP.values():
        columns.append(Column(db, _TYPES[kind], nullable=kind not in ("date", "id", "hour", "text!")))
    columns += [Column("report_id", String(36)), Column("loaded_at", DateTime, nullable=False)]
    if with_row_key:
        columns.append(Column(ROW_KEY_COLUMN, String(64), nullable=False))
        columns.append(Index("uk_row_key", ROW_KEY_COLUMN, unique=True))
    table = Table(TARGET_TABLE, metadata, *columns)
    expected = [c for c in TABLE_COLUMNS if with_row_key or c != ROW_KEY_COLUMN]
    assert [c.name for c in table.columns][1:] == expected
    return table


def make_engine(*, with_row_key: bool = True):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    metadata = MetaData()
    make_table(metadata, with_row_key=with_row_key)
    metadata.create_all(engine)
    return engine


@pytest.fixture
def engine():
    engine = make_engine()
    yield engine
    engine.dispose()


def open_database(engine, load_mode: str = "replace") -> SearchTermDatabase:
    database = SearchTermDatabase(engine, chunk_size=3)
    database.reflect()
    database.validate_schema(TABLE_COLUMNS, load_mode)
    return database


@pytest.fixture
def database(engine) -> SearchTermDatabase:
    return open_database(engine)
