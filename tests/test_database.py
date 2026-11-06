"""Slice replacement in searchtermreport (SQLite), and the MySQL DDL."""

from __future__ import annotations

import re
from datetime import date, datetime

import pytest
from sqlalchemy import MetaData, create_engine, insert, select

from app.database import DDL_FILE, SearchTermDatabase, read_ddl
from app.exceptions import DatabaseError
from app.search_term_processor import NATURAL_KEY, TABLE_COLUMNS, row_key

from .conftest import open_database

D6, D5 = date(2026, 11, 6), date(2026, 11, 5)


def _row(day: date, country: str, hour: int = 0, term: str | None = "kw", clicks: int = 1,
         target: int | None = 3) -> dict:
    row = {
        "report_date": day, "hour": hour, "campaign_id": 1, "campaign_name": "c", "campaign_country": country,
        "ad_group_id": 2, "target_id": target, "search_term": term, "impressions": 1, "clicks": clicks,
        "total_cost": 0, "purchases": 0, "purchases_multi_touch": 0, "sales": 0, "sales_multi_touch": 0,
        "units_sold": 0, "units_sold_multi_touch": 0, "loaded_at": datetime(2026, 11, 6),
    }
    row["row_key"] = row_key(tuple(row[c] for c in NATURAL_KEY))
    return row


def _seed(database: SearchTermDatabase, rows: list[dict]) -> None:
    with database.engine.begin() as conn:
        conn.execute(insert(database.table), rows)


def _all(database: SearchTermDatabase) -> list[tuple]:
    t = database.table
    with database.engine.connect() as conn:
        return sorted(conn.execute(select(t.c.report_date, t.c.campaign_country, t.c.search_term, t.c.clicks)))


def test_only_the_search_term_table_is_allowed(engine):
    with pytest.raises(DatabaseError, match="only writes"):
        SearchTermDatabase(engine, "targeting_daily")


def test_missing_table_points_to_create_table():
    database = SearchTermDatabase(create_engine("sqlite://"))
    with pytest.raises(DatabaseError, match="--create-table"):
        database.reflect()


def test_replace_touches_only_the_files_dates_and_marketplaces(database):
    _seed(database, [_row(D6, "US", term="old"), _row(D6, "GB", term="keep-gb"), _row(D5, "US", term="keep-d5")])
    records = database.to_records(
        __import__("pandas").DataFrame([_row(D6, "US", 0, "new-a"), _row(D6, "US", 1, "new-b")])
    )
    result = database.replace_slices([(D6, "US")], records)
    assert (result.deleted, result.inserted) == (1, 2)
    assert _all(database) == [
        (D5, "US", "keep-d5", 1),
        (D6, "GB", "keep-gb", 1),
        (D6, "US", "new-a", 1),
        (D6, "US", "new-b", 1),
    ]


def test_rerun_converges_instead_of_duplicating(database):
    import pandas as pd

    frame = pd.DataFrame([_row(D6, "US", h, f"t{h}") for h in range(7)])  # > chunk size 3
    for _ in range(3):
        database.replace_slices([(D6, "US")], database.to_records(frame))
    assert len(_all(database)) == 7


def test_zero_records_never_delete(database):
    _seed(database, [_row(D6, "US")])
    with pytest.raises(ValueError, match="zero records"):
        database.replace_slices([(D6, "US")], [])
    assert len(_all(database)) == 1


def test_records_outside_the_slices_are_refused(database):
    import pandas as pd

    records = database.to_records(pd.DataFrame([_row(D6, "GB")]))
    with pytest.raises(ValueError, match="outside"):
        database.replace_slices([(D6, "US")], records)


def test_failed_insert_rolls_back_the_delete(database):
    import pandas as pd

    _seed(database, [_row(D6, "US", term="old")])
    bad = _row(D6, "US", term="new")
    bad["campaign_name"] = None  # NOT NULL
    with pytest.raises(DatabaseError, match="rolled back"):
        database.replace_slices([(D6, "US")], database.to_records(pd.DataFrame([bad])))
    assert _all(database) == [(D6, "US", "old", 1)]


def test_create_table_is_skipped_when_it_exists(database):
    assert database.create_table("CREATE TABLE never_run (x int)") is False


def test_create_table_on_an_empty_database():
    engine = create_engine("sqlite://")
    database = SearchTermDatabase(engine)
    ddl = "CREATE TABLE IF NOT EXISTS searchtermreport (id INTEGER PRIMARY KEY, report_date DATE)"
    assert database.create_table(ddl) is True
    assert database.exists()


def test_mysql_ddl_matches_the_loader():
    ddl = read_ddl()
    assert ddl.lower().startswith("create table if not exists searchtermreport")
    body = ddl[ddl.index("(") + 1 :]
    names = [
        m.group(1)
        for m in re.finditer(r"^\s*([a-z_]+)\s+(?:BIGINT|TINYINT|INT|DECIMAL|VARCHAR|CHAR|DATE|DATETIME)\b", body, re.M)
    ]
    assert names[0] == "id"
    # Same columns (inserts are by name; the DDL keeps hour beside report_date).
    assert sorted(names[1:]) == sorted(TABLE_COLUMNS)
    assert len(names) == len(set(names))


def test_ddl_file_must_be_the_one_table(tmp_path):
    path = tmp_path / "x.sql"
    path.write_text("DROP TABLE searchtermreport;", encoding="utf-8")
    with pytest.raises(DatabaseError):
        read_ddl(path)
    assert DDL_FILE.is_file()
