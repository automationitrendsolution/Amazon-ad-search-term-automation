"""MySQL persistence for ``gen_ai_db.searchtermreport`` -- and nothing else.

Three load modes (``DB_LOAD_MODE`` / ``--load-mode``), each in **one
transaction** on one connection, rolled back on any error.

``replace`` (default) replaces every (report_date, campaign_country) pair the
downloaded file contains:

    BEGIN
      DELETE FROM searchtermreport
       WHERE (report_date = d1 AND campaign_country = c1) OR ...
      INSERT ... the prepared rows (chunked) ...
      SELECT COUNT(*) ... same WHERE ...          -- must equal inserted
    COMMIT            -- or ROLLBACK, leaving the previous rows intact

Hourly re-runs of the Today report therefore overwrite the day so far instead
of adding to it, and a retry after a lost connection cannot duplicate data.
Marketplaces and dates that are not in the file are never touched.

``upsert``, per chunk::

      INSERT ... ON DUPLICATE KEY UPDATE <every column but row_key>
      SELECT COUNT(*) ... WHERE row_key IN (chunk)  -- must equal the chunk

New rows are added; rows already in the table (same ``row_key``) get the new
values.  Nothing is deleted, so a row Amazon later drops from the report stays.

``insert`` is like upsert, but rows already in the table are left unchanged
(``ON DUPLICATE KEY UPDATE id = id``: a no-op that, unlike ``INSERT IGNORE``,
still raises every other error).

Upsert and insert need the ``row_key`` column with its UNIQUE index
(``sql/migrate_add_row_key.sql`` for a table created before it existed).

The table is created once with ``python run.py --create-table`` (the DDL in
``sql/create_searchtermreport.sql``); the hourly load only reflects it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import (
    BigInteger,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    and_,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import (
    ALLOWED_TABLES,
    KEYED_LOAD_MODES,
    LOAD_MODES,
    PROJECT_ROOT,
    ROW_KEY_COLUMN,
    TARGET_TABLE,
    DatabaseConfig,
)
from .exceptions import DatabaseError
from .logging_config import get_logger

logger = get_logger("database")

DDL_FILE = PROJECT_ROOT / "sql" / "create_searchtermreport.sql"
MIGRATION_FILE = "sql/migrate_add_row_key.sql"

#: MySQL errors a second attempt can plausibly get past.
TRANSIENT_MYSQL_ERRNOS = frozenset({1205, 1213, 2003, 2006, 2013, 2055})


def _database_error(message: str, exc: SQLAlchemyError) -> DatabaseError:
    """Wrap a driver failure, marking it transient when a retry could succeed."""
    error = DatabaseError(message)
    errno = getattr(getattr(exc, "orig", None), "errno", None)
    error.transient = bool(
        getattr(exc, "connection_invalidated", False) or errno in TRANSIENT_MYSQL_ERRNOS
    )
    return error


@dataclass(frozen=True)
class WriteResult:
    table: str
    slices: tuple[tuple[date, str], ...]
    deleted: int
    inserted: int
    mode: str = "replace"
    #: upsert: rows already in the table, overwritten with the file's values.
    updated: int = 0
    #: insert: rows already in the table, left unchanged.
    skipped: int = 0

    def describe(self) -> str:
        if self.mode == "upsert":
            return f"upsert: {self.inserted} inserted, {self.updated} updated"
        if self.mode == "insert":
            return f"insert: {self.inserted} inserted, {self.skipped} already present (unchanged)"
        return f"replace: {self.deleted} deleted, {self.inserted} inserted"


def create_db_engine(config: DatabaseConfig, *, echo: bool = False) -> Engine:
    """Pooled engine.  The URL is never logged with its password."""
    try:
        return create_engine(
            config.url,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=config.pool_recycle_seconds,
            connect_args=config.connect_args(),
        )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"Could not create engine for {config.safe_url}: {exc}") from exc


def read_ddl(path: Path = DDL_FILE) -> str:
    """The CREATE TABLE statement, without comments.  Only this table may be created."""
    statement = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("--")
    ).strip().rstrip(";")
    head = " ".join(statement.split()[:6]).lower()
    if not head.startswith("create table if not exists") or TARGET_TABLE.lower() not in head:
        raise DatabaseError(f"{path} must hold one CREATE TABLE IF NOT EXISTS {TARGET_TABLE} statement")
    if ";" in statement:
        raise DatabaseError(f"{path} must hold exactly one statement")
    return statement


class SearchTermDatabase:
    """Schema checks and slice replacement for the one allowed table."""

    def __init__(
        self, engine: Engine, table_name: str = TARGET_TABLE, *, chunk_size: int = 2000, commit_every: int = 0
    ) -> None:
        if table_name not in ALLOWED_TABLES:
            raise DatabaseError(
                f"Refusing to use table {table_name!r}: this project only writes "
                f"{', '.join(sorted(ALLOWED_TABLES))}"
            )
        self.engine = engine
        self.table_name = table_name
        self.chunk_size = chunk_size
        #: upsert / insert: rows per committed batch (0 = one transaction).
        self.commit_every = commit_every
        self._table: Table | None = None
        self.integer_columns: frozenset[str] = frozenset()
        #: Columns written; set by validate_schema (row_key is left out when a
        #: replace load runs against a table that predates it).
        self.write_columns: tuple[str, ...] | None = None

    # -- schema -------------------------------------------------------------

    def check_connection(self) -> None:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise _database_error(f"Database connection failed: {exc}", exc) from exc
        logger.info("Database connection verified")

    def exists(self) -> bool:
        try:
            return inspect(self.engine).has_table(self.table_name)
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not inspect the database: {exc}", exc) from exc

    def create_table(self, ddl: str | None = None) -> bool:
        """Run the CREATE TABLE IF NOT EXISTS statement.  Returns True if it was created."""
        existed = self.exists()
        if existed:
            logger.info("%s already exists; not changed", self.table_name)
            return False
        try:
            with self.engine.begin() as connection:
                connection.execute(text(ddl or read_ddl()))
        except SQLAlchemyError as exc:
            raise _database_error(f"CREATE TABLE {self.table_name} failed: {exc}", exc) from exc
        self._table = None
        logger.info("Created table %s", self.table_name)
        return True

    def reflect(self) -> Table:
        if not self.exists():
            raise DatabaseError(
                f"Table {self.table_name!r} does not exist. Create it once with: python run.py --create-table"
            )
        try:
            self._table = Table(self.table_name, MetaData(), autoload_with=self.engine)
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not reflect {self.table_name!r}: {exc}", exc) from exc
        logger.info("Reflected %s with %d column(s)", self.table_name, len(self._table.columns))
        return self._table

    @property
    def table(self) -> Table:
        return self._table if self._table is not None else self.reflect()

    def has_unique_row_key(self) -> bool:
        """True when ``row_key`` has a UNIQUE index or constraint of its own."""
        try:
            inspector = inspect(self.engine)
            unique = [i["column_names"] for i in inspector.get_indexes(self.table_name) if i.get("unique")]
            try:
                unique += [c["column_names"] for c in inspector.get_unique_constraints(self.table_name)]
            except NotImplementedError:
                pass
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not inspect indexes of {self.table_name}: {exc}", exc) from exc
        return any(list(columns) == [ROW_KEY_COLUMN] for columns in unique)

    def validate_schema(self, expected_columns: Sequence[str], load_mode: str = "replace") -> None:
        """The table must hold every prepared column; extras must be optional.

        ``row_key`` with its UNIQUE index is required for upsert / insert.  A
        replace load against a table without the column writes the others.

        Raises:
            DatabaseError: A prepared column is missing, a required column
                cannot be filled, or the load mode needs ``row_key``.
        """
        if load_mode not in LOAD_MODES:
            raise DatabaseError(f"Unknown load mode {load_mode!r}; use one of {', '.join(LOAD_MODES)}")
        live = {c.name: c for c in self.table.columns}
        expected_columns = list(expected_columns)
        if ROW_KEY_COLUMN in expected_columns:
            if load_mode in KEYED_LOAD_MODES and not (ROW_KEY_COLUMN in live and self.has_unique_row_key()):
                raise DatabaseError(
                    f"DB_LOAD_MODE={load_mode} needs {self.table_name}.{ROW_KEY_COLUMN} with a UNIQUE "
                    f"index. Have an admin run {MIGRATION_FILE} once, or use DB_LOAD_MODE=replace."
                )
            if ROW_KEY_COLUMN not in live:
                logger.warning(
                    "%s has no %s column; the replace load continues without it (run %s to enable "
                    "upsert / insert)", self.table_name, ROW_KEY_COLUMN, MIGRATION_FILE,
                )
                expected_columns.remove(ROW_KEY_COLUMN)
        missing = [c for c in expected_columns if c not in live]
        if missing:
            raise DatabaseError(f"{self.table_name} is missing column(s) the report writes: {', '.join(missing)}")
        blocking = [
            name
            for name, column in live.items()
            if name not in expected_columns
            and not (column.primary_key and column.autoincrement in (True, "auto"))
            and not column.nullable
            and column.server_default is None
            and column.default is None
        ]
        if blocking:
            raise DatabaseError(
                f"{self.table_name} has NOT NULL column(s) without a default that the report "
                f"does not fill: {', '.join(blocking)}"
            )
        self.integer_columns = frozenset(
            c.name
            for c in self.table.columns
            if c.name in expected_columns and isinstance(c.type, (Integer, BigInteger, SmallInteger))
        )
        self.write_columns = tuple(expected_columns)
        logger.info(
            "Schema validated: %d column(s) match %s (load mode: %s)",
            len(expected_columns), self.table_name, load_mode,
        )

    # -- writes -------------------------------------------------------------

    def _criteria(self, slices: Sequence[tuple[date, str]]):
        table = self.table
        return or_(*(and_(table.c.report_date == d, table.c.campaign_country == c) for d, c in slices))

    def to_records(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        """Rows as driver-ready dicts: pandas NA -> NULL, numpy scalars -> Python."""
        records: list[dict[str, object]] = []
        integer_columns = self.integer_columns
        if self.write_columns is not None:
            frame = frame.loc[:, [c for c in frame.columns if c in self.write_columns]]
        for row in frame.to_dict(orient="records"):
            record: dict[str, object] = {}
            for key, value in row.items():
                if value is None or value is pd.NA or value is pd.NaT:
                    value = None
                elif isinstance(value, (float, np.floating)):
                    value = None if np.isnan(value) else float(value)
                    if value is not None and key in integer_columns:
                        value = int(round(value))
                elif isinstance(value, np.integer):
                    value = int(value)
                elif isinstance(value, pd.Timestamp):
                    value = value.to_pydatetime()
                record[key] = value
            records.append(record)
        return records

    def lazy_records(self, frame: pd.DataFrame) -> "FrameRecords":
        """Like :meth:`to_records`, but converted one chunk at a time while writing.

        A full "Last week" file is ~1.35 million rows; as dicts all at once that
        is several GB, while one chunk is a few MB.
        """
        return FrameRecords(self, frame.reset_index(drop=True))

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Explicit BEGIN / COMMIT on one connection, ROLLBACK on any error."""
        connection = self.engine.connect()
        try:
            trans = connection.begin()
            try:
                yield connection
            except Exception as exc:
                try:
                    trans.rollback()
                except SQLAlchemyError as rollback_exc:
                    logger.warning("ROLLBACK failed: %s", rollback_exc)
                logger.error("ROLLBACK after error: %s: %s", type(exc).__name__, exc)
                raise
            trans.commit()
        finally:
            connection.close()

    def replace_slices(
        self, slices: Sequence[tuple[date, str]], records: Sequence[dict[str, object]]
    ) -> WriteResult:
        """Replace every (report_date, campaign_country) in ``slices`` atomically.

        Raises:
            ValueError: No records, or records outside ``slices``.
            DatabaseError: Any database failure -- rolled back first.
        """
        if not records:
            raise ValueError(f"{self.table_name}: zero records; refusing to delete data")
        if not slices:
            raise ValueError(f"{self.table_name}: no dates given")
        allowed = set(slices)
        stray = set(zip(_column(records, "report_date"), _column(records, "campaign_country"))) - allowed
        if stray:
            raise ValueError(f"{self.table_name}: records outside the replaced dates/countries: {sorted(stray)[:5]}")

        table = self.table
        label = ", ".join(f"{d.isoformat()}/{c}" for d, c in slices)
        try:
            with self.transaction() as connection:
                criteria = self._criteria(slices)
                deleted = connection.execute(delete(table).where(criteria)).rowcount
                inserted = 0
                for start in range(0, len(records), self.chunk_size):
                    chunk = list(records[start : start + self.chunk_size])
                    connection.execute(insert(table), chunk)
                    inserted += len(chunk)
                present = connection.execute(
                    select(func.count()).select_from(table).where(criteria)
                ).scalar_one()
                if present != inserted:
                    raise DatabaseError(
                        f"After insert {present} row(s) exist for {label}, expected {inserted}; rolling back"
                    )
        except SQLAlchemyError as exc:
            raise _database_error(f"Load for {label} failed and was rolled back: {exc}", exc) from exc
        logger.info("Committed %s: %s deleted, %s inserted (%s)", self.table_name, deleted, inserted, label)
        return WriteResult(self.table_name, tuple(slices), max(int(deleted), 0), inserted)

    def write(
        self, mode: str, slices: Sequence[tuple[date, str]], records: Sequence[dict[str, object]]
    ) -> WriteResult:
        """Write one validated report with the given load mode."""
        if mode == "replace":
            return self.replace_slices(slices, records)
        if mode in KEYED_LOAD_MODES:
            return self.merge_rows(slices, records, update_existing=mode == "upsert")
        raise ValueError(f"Unknown load mode {mode!r}; use one of {', '.join(LOAD_MODES)}")

    def _merge_statement(self, columns: Sequence[str], *, update_existing: bool):
        """INSERT that updates (upsert) or keeps (insert) a row whose row_key already exists."""
        table = self.table
        dialect = self.engine.dialect.name
        updates = [c for c in columns if c != ROW_KEY_COLUMN]
        if dialect in ("mysql", "mariadb"):
            stmt = mysql.insert(table)
            if update_existing:
                return stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in updates})
            return stmt.on_duplicate_key_update({"id": table.c.id})
        if dialect == "sqlite":  # the test suite
            stmt = sqlite.insert(table)
            if update_existing:
                return stmt.on_conflict_do_update(
                    index_elements=[ROW_KEY_COLUMN], set_={c: stmt.excluded[c] for c in updates}
                )
            return stmt.on_conflict_do_nothing(index_elements=[ROW_KEY_COLUMN])
        raise DatabaseError(f"Upsert / insert loads are not supported on {dialect}")

    def merge_rows(
        self,
        slices: Sequence[tuple[date, str]],
        records: Sequence[dict[str, object]],
        *,
        update_existing: bool,
    ) -> WriteResult:
        """Upsert (``update_existing``) or insert-new-only, matched on row_key.

        With ``commit_every`` = 0 the whole write is one transaction.  Otherwise
        every ``commit_every`` rows are committed on their own: a big backfill
        then survives a dropped connection, and running it again continues where
        it stopped (both modes only add or overwrite rows, so a repeated batch
        changes nothing; insert mode skips a chunk that is already stored
        without sending it again).

        Raises:
            ValueError: No records, a record without row_key, or a repeated row_key.
            DatabaseError: Any database failure -- the open batch is rolled back.
        """
        mode = "upsert" if update_existing else "insert"
        if not records:
            raise ValueError(f"{self.table_name}: zero records")
        keys = _column(records, ROW_KEY_COLUMN)
        if not all(keys):
            raise ValueError(f"{self.table_name}: {mode} needs {ROW_KEY_COLUMN} on every record")
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.table_name}: the records repeat a {ROW_KEY_COLUMN}")

        table = self.table
        key_column = table.c[ROW_KEY_COLUMN]
        statement = self._merge_statement(list(records[0]), update_existing=update_existing)
        label = ", ".join(f"{d.isoformat()}/{c}" for d, c in slices) or "-"
        total = len(records)
        if self.commit_every:
            batch = max(self.chunk_size, self.commit_every - self.commit_every % self.chunk_size)
        else:
            batch = total
        inserted = existing = committed = 0
        try:
            for batch_start in range(0, total, batch):
                batch_end = min(batch_start + batch, total)
                batch_inserted = batch_existing = 0
                with self.transaction() as connection:
                    for start in range(batch_start, batch_end, self.chunk_size):
                        stop = min(start + self.chunk_size, batch_end)
                        count = select(func.count()).select_from(table).where(key_column.in_(keys[start:stop]))
                        already = int(connection.execute(count).scalar_one())
                        if not (already == stop - start and not update_existing):
                            connection.execute(statement, list(records[start:stop]))
                            present = int(connection.execute(count).scalar_one())
                            if present != stop - start:
                                raise DatabaseError(
                                    f"After {mode} {present} of {stop - start} row(s) exist; rolling back"
                                )
                        batch_existing += already
                        batch_inserted += stop - start - already
                committed = batch_end
                inserted += batch_inserted
                existing += batch_existing
                if batch < total:
                    logger.info(
                        "%s progress: %d / %d row(s) committed (%d%%)",
                        mode, committed, total, committed * 100 // total,
                    )
        except SQLAlchemyError as exc:
            kept = (
                f"; {committed} of {total} row(s) were already committed and stay -- run again to continue"
                if committed else " and was rolled back"
            )
            raise _database_error(f"{mode.capitalize()} for {label} failed{kept}: {exc}", exc) from exc
        except DatabaseError as exc:
            if committed:
                raise DatabaseError(f"{exc} ({committed} of {total} row(s) were already committed)") from exc
            raise
        result = WriteResult(
            self.table_name,
            tuple(slices),
            0,
            inserted,
            mode,
            updated=existing if update_existing else 0,
            skipped=0 if update_existing else existing,
        )
        logger.info("Committed %s: %s (%s)", self.table_name, result.describe(), label)
        return result

    def count_for(self, slices: Sequence[tuple[date, str]]) -> int:
        try:
            with self.engine.connect() as connection:
                return int(
                    connection.execute(
                        select(func.count()).select_from(self.table).where(self._criteria(slices))
                    ).scalar_one()
                )
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not count rows: {exc}", exc) from exc

    def dispose(self) -> None:
        self.engine.dispose()


class FrameRecords(Sequence):
    """A prepared DataFrame seen as a sequence of driver-ready records.

    Slicing converts only that slice (via ``SearchTermDatabase.to_records``),
    so a write holds one chunk of dicts in memory at a time.
    """

    def __init__(self, database: SearchTermDatabase, frame: pd.DataFrame) -> None:
        self.database = database
        self.frame = frame

    def __len__(self) -> int:
        return len(self.frame)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self.database.to_records(self.frame.iloc[index])
        if index < 0:
            index += len(self.frame)
        if not 0 <= index < len(self.frame):
            raise IndexError(index)
        return self.database.to_records(self.frame.iloc[index : index + 1])[0]

    def column(self, name: str) -> list[object]:
        if name not in self.frame.columns:
            return [None] * len(self.frame)
        return [None if v is pd.NA else v for v in self.frame[name].tolist()]


def _column(records: Sequence[dict[str, object]], name: str) -> list[object]:
    """One field of every record, without converting a FrameRecords row by row."""
    if isinstance(records, FrameRecords):
        return records.column(name)
    return [r.get(name) for r in records]


def build_database(
    config: DatabaseConfig,
    columns: Sequence[str],
    *,
    chunk_size: int = 2000,
    commit_every: int = 0,
    echo: bool = False,
    validate: bool = True,
    load_mode: str = "replace",
) -> SearchTermDatabase:
    """Connect and (unless ``validate`` is False) check the live table before anything is downloaded."""
    engine = create_db_engine(config, echo=echo)
    try:
        database = SearchTermDatabase(engine, chunk_size=chunk_size, commit_every=commit_every)
        database.check_connection()
        if validate:
            database.reflect()
            database.validate_schema(columns, load_mode)
    except Exception:
        engine.dispose()
        raise
    return database

