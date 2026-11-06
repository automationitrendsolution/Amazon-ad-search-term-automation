"""DB_LOAD_MODE upsert / insert (SQLite), row_key, and the pipeline wiring."""

from __future__ import annotations

import hashlib
from datetime import date, datetime

import pandas as pd
import pytest
from sqlalchemy import func, select

from app.config import load_settings
from app.exceptions import ConfigurationError, DatabaseError
from app.main import RunMode, RunOptions, SearchTermPipeline
from app.search_term_processor import NATURAL_KEY, read_search_term_report, row_key, transform_search_term_report

from .conftest import csv_text, line, make_engine, open_database
from .test_database import _row, _seed
from .test_pipeline import EVENING_LA, FakeDownloads

D6, D5 = date(2026, 11, 6), date(2026, 11, 5)


def _rows(database) -> list[tuple]:
    t = database.table
    with database.engine.connect() as conn:
        return sorted(conn.execute(select(t.c.report_date, t.c.campaign_country, t.c.search_term, t.c.clicks)))


def _records(database, rows: list[dict]) -> list[dict]:
    return database.to_records(pd.DataFrame(rows))


# -- row_key ------------------------------------------------------------------


def test_row_key_matches_the_mysql_backfill_formula():
    # sql/migrate_add_row_key.sql: SHA2(CONCAT(date,'|',hour,'|',country,'|',campaign,'|',
    #   COALESCE(ad_group,''),'|',COALESCE(target,''),'|',COALESCE(search_term,'')), 256)
    values = (D6, 7, "GB", 141242384338773, 188245137651693, None, "regenponcho damen")
    expected = hashlib.sha256(
        "2026-11-06|7|GB|141242384338773|188245137651693||regenponcho damen".encode("utf-8")
    ).hexdigest()
    assert row_key(values) == expected
    assert row_key((D6, 7, "GB", 1, None, None, "ä")) == hashlib.sha256("2026-11-06|7|GB|1|||ä".encode()).hexdigest()


def test_transform_adds_one_row_key_per_natural_key(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(csv_text([line(hour=h) for h in range(3)] + [line(target="", term="Shoes")]), encoding="utf-8")
    report = transform_search_term_report(
        read_search_term_report(path), report_id="r", loaded_at=datetime(2026, 11, 6), today=None, max_age_days=8
    )
    frame = report.frame
    assert frame["row_key"].str.fullmatch(r"[0-9a-f]{64}").all()
    assert frame["row_key"].is_unique
    first = frame.iloc[0]
    assert first["row_key"] == row_key(tuple(first[c] for c in NATURAL_KEY))


# -- upsert ------------------------------------------------------------------


def test_upsert_inserts_new_rows_and_updates_existing_ones():
    database = open_database(make_engine(), "upsert")
    _seed(database, [_row(D6, "US", 0, "a", clicks=1), _row(D5, "US", 0, "keep", clicks=9)])
    result = database.write("upsert", [(D6, "US")], _records(database, [
        _row(D6, "US", 0, "a", clicks=5),   # existing -> updated
        _row(D6, "US", 1, "b", clicks=2),   # new
        _row(D6, "GB", 0, "c", clicks=3),   # new
        _row(D6, "GB", 1, "d", clicks=4),   # new; > chunk size 3
    ]))
    assert (result.mode, result.inserted, result.updated, result.deleted) == ("upsert", 3, 1, 0)
    assert result.describe() == "upsert: 3 inserted, 1 updated"
    assert _rows(database) == [
        (D5, "US", "keep", 9),
        (D6, "GB", "c", 3),
        (D6, "GB", "d", 4),
        (D6, "US", "a", 5),
        (D6, "US", "b", 2),
    ]


def test_upsert_rerun_converges_instead_of_duplicating():
    database = open_database(make_engine(), "upsert")
    rows = [_row(D6, "US", h, f"t{h}") for h in range(7)]
    database.write("upsert", [(D6, "US")], _records(database, rows))
    again = database.write("upsert", [(D6, "US")], _records(database, rows))
    assert (again.inserted, again.updated) == (0, 7)
    assert len(_rows(database)) == 7


def test_upsert_matches_rows_without_a_target_id():
    # Sponsored Brands rows have no Target ID: a plain UNIQUE index would never match them.
    database = open_database(make_engine(), "upsert")
    database.write("upsert", [(D6, "US")], _records(database, [_row(D6, "US", target=None, clicks=1)]))
    result = database.write("upsert", [(D6, "US")], _records(database, [_row(D6, "US", target=None, clicks=8)]))
    assert (result.inserted, result.updated) == (0, 1)
    assert _rows(database) == [(D6, "US", "kw", 8)]


def test_search_terms_differing_only_in_case_are_separate_rows():
    database = open_database(make_engine(), "upsert")
    result = database.write("upsert", [(D6, "US")], _records(database, [
        _row(D6, "US", term="Shoes"), _row(D6, "US", term="shoes"),
    ]))
    assert result.inserted == 2


def test_failed_upsert_rolls_back_every_chunk():
    database = open_database(make_engine(), "upsert")
    _seed(database, [_row(D6, "US", 0, "a", clicks=1)])
    rows = [_row(D6, "US", 0, "a", clicks=5), _row(D6, "US", 1, "b"), _row(D6, "US", 2, "c"), _row(D6, "US", 3, "d")]
    rows[3]["campaign_name"] = None  # NOT NULL, in the second chunk
    with pytest.raises(DatabaseError, match="rolled back"):
        database.write("upsert", [(D6, "US")], _records(database, rows))
    assert _rows(database) == [(D6, "US", "a", 1)]


def test_repeated_row_keys_are_refused():
    database = open_database(make_engine(), "upsert")
    with pytest.raises(ValueError, match="repeat"):
        database.write("upsert", [(D6, "US")], _records(database, [_row(D6, "US"), _row(D6, "US")]))


# -- insert ------------------------------------------------------------------


def test_insert_adds_new_rows_and_leaves_existing_ones_unchanged():
    database = open_database(make_engine(), "insert")
    _seed(database, [_row(D6, "US", 0, "a", clicks=1)])
    result = database.write("insert", [(D6, "US")], _records(database, [
        _row(D6, "US", 0, "a", clicks=99),  # already there -> kept as is
        _row(D6, "US", 1, "b", clicks=2),
    ]))
    assert (result.mode, result.inserted, result.skipped, result.updated) == ("insert", 1, 1, 0)
    assert result.describe() == "insert: 1 inserted, 1 already present (unchanged)"
    assert _rows(database) == [(D6, "US", "a", 1), (D6, "US", "b", 2)]


# -- batched commits -----------------------------------------------------------


def test_batches_commit_separately_and_a_rerun_continues(monkeypatch):
    database = open_database(make_engine(), "insert")
    database.commit_every = 4  # rounded down to whole chunks of 3 -> batches of 3
    rows = [_row(D6, "US", h, f"t{h}") for h in range(8)]
    records = _records(database, rows)

    real = database.transaction
    opened = []

    def failing_third_batch():
        opened.append(1)
        if len(opened) == 3:
            raise DatabaseError("connection lost")
        return real()

    monkeypatch.setattr(database, "transaction", failing_third_batch)
    with pytest.raises(DatabaseError, match="6 of 8 row"):
        database.write("insert", [(D6, "US")], records)
    assert len(_rows(database)) == 6  # the first two batches stayed

    monkeypatch.setattr(database, "transaction", real)
    result = database.write("insert", [(D6, "US")], records)
    assert (result.inserted, result.skipped) == (2, 6)
    assert len(_rows(database)) == 8


def test_db_commit_every_rows_setting(env, monkeypatch):
    assert load_settings().commit_every_rows == 50_000
    monkeypatch.setenv("DB_COMMIT_EVERY_ROWS", "0")
    assert load_settings().commit_every_rows == 0


# -- chunk-by-chunk conversion ---------------------------------------------------


@pytest.mark.parametrize("mode", ["replace", "upsert", "insert"])
def test_lazy_records_write_the_same_rows_as_a_list(mode):
    rows = [_row(D6, "US", h, f"t{h}", clicks=h) for h in range(7)]  # > chunk size 3
    eager = open_database(make_engine(), mode)
    lazy = open_database(make_engine(), mode)
    expected = eager.write(mode, [(D6, "US")], _records(eager, rows))
    records = lazy.lazy_records(pd.DataFrame(rows))
    assert len(records) == 7 and records[-1] == _records(lazy, rows)[-1]
    result = lazy.write(mode, [(D6, "US")], records)
    assert (result.inserted, result.deleted) == (expected.inserted, expected.deleted)
    assert _rows(lazy) == _rows(eager)


def test_lazy_records_outside_the_slices_are_refused():
    database = open_database(make_engine())
    with pytest.raises(ValueError, match="outside"):
        database.write("replace", [(D6, "US")], database.lazy_records(pd.DataFrame([_row(D6, "GB")])))


# -- tables created before row_key ---------------------------------------------


@pytest.mark.parametrize("mode", ["upsert", "insert"])
def test_keyed_modes_need_the_row_key_migration(mode):
    with pytest.raises(DatabaseError, match="migrate_add_row_key.sql"):
        open_database(make_engine(with_row_key=False), mode)


def test_replace_still_works_on_a_table_without_row_key():
    database = open_database(make_engine(with_row_key=False), "replace")
    records = _records(database, [_row(D6, "US", 0, "a"), _row(D6, "US", 1, "b")])
    assert all("row_key" not in r for r in records)
    assert database.write("replace", [(D6, "US")], records).inserted == 2


# -- configuration and pipeline --------------------------------------------------


def test_db_load_mode_is_validated(env, monkeypatch):
    assert load_settings().load_mode == "replace"
    monkeypatch.setenv("DB_LOAD_MODE", "UPSERT")
    assert load_settings().load_mode == "upsert"
    monkeypatch.setenv("DB_LOAD_MODE", "merge")
    with pytest.raises(ConfigurationError, match="DB_LOAD_MODE"):
        load_settings()


def _run(settings, engine, downloads, mode: str):
    return SearchTermPipeline(
        settings,
        RunOptions(mode=RunMode.LOAD, use_lock=False, load_mode=mode),
        database_factory=lambda: open_database(engine, mode),
        download_fn=downloads,
        reference_time=EVENING_LA,
    ).run()


def test_download_latest_loads_a_run_of_any_period(settings):
    engine = make_engine()
    last_week = csv_text([line(day="Oct 26, 2026", country="US", hour=h) for h in range(3)])
    refused = FakeDownloads(settings, {"today": last_week})
    with pytest.raises(Exception):  # older than MAX_REPORT_AGE_DAYS: refused without --latest
        _run(settings, engine, refused, "upsert")
    pipeline = SearchTermPipeline(
        settings,
        RunOptions(mode=RunMode.LOAD, use_lock=False, load_mode="upsert", latest=True),
        database_factory=lambda: open_database(engine, "upsert"),
        download_fn=FakeDownloads(settings, {"today": last_week}),
        reference_time=EVENING_LA,
    )
    assert pipeline.run().saved_reports[0].write.inserted == 3


def test_hourly_upsert_updates_the_day_so_far(settings):
    engine = make_engine()
    downloads = FakeDownloads(settings, {"today": csv_text(
        [line(day="Nov 05, 2026", country="US", hour=h, clicks=1) for h in range(3)]
    )})
    summary = _run(settings, engine, downloads, "upsert")
    assert summary.saved_reports[0].write.inserted == 3

    downloads.contents["today"] = csv_text(
        [line(day="Nov 05, 2026", country="US", hour=h, clicks=2) for h in range(5)]
    )
    write = _run(settings, engine, downloads, "upsert").saved_reports[0].write
    assert (write.mode, write.inserted, write.updated) == ("upsert", 2, 3)
    database = open_database(engine, "upsert")
    with engine.connect() as conn:
        count, clicks = conn.execute(
            select(func.count(), func.sum(database.table.c.clicks)).select_from(database.table)
        ).one()
    assert (count, clicks) == (5, 10)
