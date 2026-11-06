"""Environment-driven configuration.

Every secret arrives through the process environment (populated from a
mode-600 ``.env`` file, or exported by the scheduler).  Validation is strict and
fails fast: a run with a bad timezone or report ID never opens a browser.

The destination is fixed: database ``gen_ai_db``, table
``searchtermreport``.  Neither is configurable to anything else, so no
setting can point this workflow at another table.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: The only database and table this project is allowed to write to.
TARGET_DATABASE = "gen_ai_db"
TARGET_TABLE = "searchtermreport"
ALLOWED_TABLES = frozenset({TARGET_TABLE})

#: SHA-256 of a row's natural key (date / hour / marketplace / campaign /
#: ad group / target / search term); UNIQUE in the table.  Upsert and insert
#: loads match existing rows on it.
ROW_KEY_COLUMN = "row_key"

#: How a validated report is written (DB_LOAD_MODE / --load-mode):
#:   replace -- DELETE the file's date+marketplace pairs, then INSERT (the original behaviour)
#:   upsert  -- INSERT new rows, UPDATE rows whose row_key already exists
#:   insert  -- INSERT new rows only; rows already in the table are left unchanged
LOAD_MODES: tuple[str, ...] = ("replace", "upsert", "insert")
KEYED_LOAD_MODES = frozenset({"upsert", "insert"})

UTC = ZoneInfo("UTC")

#: Used when AMAZON_ENTITY_ID is set: the advertiser account the report belongs to.
DEFAULT_HISTORY_URL_TEMPLATE = (
    "https://advertising.amazon.de/reporting/history?entityId={entity_id}&reportId={report_id}"
)
#: Without an entity: the multi-account ("All accounts") console on
#: advertising.amazon.de that the search_term_Automation report was created in.
LEGACY_HISTORY_URL_TEMPLATE = (
    "https://advertising.amazon.de/reporting/history"
    "?reportId={report_id}&ephemeralAdvertiser=true"
)
ENTITY_ID_PATTERN = re.compile(r"^ENTITY[A-Z0-9]+$")

#: Amazon report IDs, as seen in reporting/history?reportId=..., are UUIDs.
REPORT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false), got {raw!r}")


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _resolve_path(raw: str | None, default: Path) -> Path:
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _get_path(name: str, default: Path) -> Path:
    return _resolve_path(_get(name), default)


def _get_zone(name: str, default: str | None) -> ZoneInfo:
    raw = _get(name, default)
    if not raw:
        raise ConfigurationError(
            f"Required environment variable {name} is not set (an IANA timezone, "
            "e.g. Asia/Kolkata)"
        )
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"{name}={raw!r} is not a valid IANA timezone. "
            "Install the 'tzdata' package if the system zone database is missing."
        ) from exc


def _get_report_id(name: str) -> str | None:
    raw = _get(name)
    if raw is None:
        return None
    if not REPORT_ID_PATTERN.match(raw):
        raise ConfigurationError(
            f"{name}={raw!r} is not a report ID. Copy the reportId=... value (a UUID) "
            "from the report's reporting/history URL."
        )
    return raw.lower()


_DEFAULT_BROWSER_ARGS: tuple[str, ...] = ("--disable-dev-shm-usage",)


def _browser_args() -> tuple[str, ...]:
    args = list(_DEFAULT_BROWSER_ARGS)
    extra = _get("BROWSER_ARGS")
    if extra:
        args.extend(part.strip() for part in extra.split(",") if part.strip())
    if _get_bool("BROWSER_NO_SANDBOX", False):
        args.append("--no-sandbox")
    return tuple(dict.fromkeys(args))


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = 30
    ssl_ca: Path | None = None
    ssl_verify_cert: bool = False
    ssl_disabled: bool = False

    def connect_args(self) -> dict[str, object]:
        """Driver kwargs for mysql-connector-python."""
        args: dict[str, object] = {"connection_timeout": self.connect_timeout_seconds}
        if self.ssl_disabled:
            args["ssl_disabled"] = True
            return args
        if self.ssl_ca is not None:
            args["ssl_ca"] = str(self.ssl_ca)
            args["ssl_verify_cert"] = self.ssl_verify_cert
        return args

    @property
    def url(self) -> str:
        """SQLAlchemy URL.  Credentials are percent-encoded, never logged."""
        return (
            f"mysql+mysqlconnector://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.name}?charset=utf8mb4"
        )

    @property
    def safe_url(self) -> str:
        """Same URL with the password redacted -- this is the loggable form."""
        return f"mysql+mysqlconnector://{self.user}:***@{self.host}:{self.port}/{self.name}"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.safe_url


@dataclass(frozen=True)
class Settings:
    """Fully validated runtime configuration."""

    database: DatabaseConfig

    #: reporting/history URL with a ``{report_id}`` placeholder.
    report_history_url_template: str
    #: Advertiser account (``entityId=`` in the console URL), when the report
    #: belongs to a single account rather than "All accounts".
    amazon_entity_id: str | None
    #: The saved search_term_Automation report (period "Today"), run every hour.
    report_id_today: str | None
    #: Optional second saved report with period "Yesterday", run in the first
    #: ``yesterday_finalize_hours`` hours of the day to complete the day that
    #: just ended (its last hour and late attributions).
    report_id_yesterday: str | None
    #: Optional text the report page must show, guarding against a swapped ID.
    report_name_today: str | None
    report_name_yesterday: str | None
    yesterday_finalize_hours: int

    amazon_email: str | None
    amazon_password: str | None = field(repr=False)
    auto_login: bool

    #: Operator's timezone: only used to show local time in the logs.
    report_timezone: ZoneInfo
    #: The browser's timezone, and the calendar "today" is judged in.  The
    #: westernmost marketplace in the report (US/CA = America/Los_Angeles), so
    #: no account's day is still running when that zone's day has ended.
    amazon_account_timezone: ZoneInfo
    #: A downloaded file may not contain dates older than this many days
    #: (covers a report period up to "Last week" / "Last 7 days").
    max_report_age_days: int
    #: Optional explicit format of the CSV's Date column.
    csv_date_format: str | None

    reports_dir: Path
    download_dir: Path
    archive_dir: Path
    failed_dir: Path
    log_dir: Path
    screenshot_dir: Path
    session_dir: Path
    lock_file: Path

    headless: bool
    browser_timeout_ms: int
    download_timeout_ms: int
    slow_mo_ms: int
    user_agent: str | None
    browser_args: tuple[str, ...]
    viewport_width: int
    viewport_height: int

    report_ready_timeout_seconds: int
    report_poll_interval_seconds: int

    max_retries: int
    retry_backoff_seconds: float
    retry_backoff_max_seconds: float

    insert_chunk_size: int
    #: upsert / insert: rows per committed batch (0 = the whole file in one transaction).
    commit_every_rows: int
    #: One of LOAD_MODES.
    load_mode: str
    #: Download the newest finished run ("Download latest") instead of
    #: clicking Run and waiting; its rows are loaded whatever their dates.
    download_latest: bool
    retention_days: int
    log_level: str
    log_retention_days: int

    @property
    def table(self) -> str:
        return TARGET_TABLE

    @property
    def has_credentials(self) -> bool:
        return bool(self.amazon_email and self.amazon_password)

    @property
    def can_auto_login(self) -> bool:
        return self.auto_login and self.has_credentials

    def history_url(self, report_id: str) -> str:
        return self.report_history_url_template.format(
            report_id=report_id, entity_id=self.amazon_entity_id or ""
        )

    def ensure_directories(self) -> None:
        """Create every directory the workflow writes to.  Idempotent."""
        for path in (
            self.download_dir,
            self.reports_dir,
            self.archive_dir,
            self.failed_dir,
            self.log_dir,
            self.screenshot_dir,
            self.session_dir,
            self.lock_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """Loggable snapshot of the configuration.  Contains no secrets."""
        return {
            "database": self.database.safe_url,
            "table": f"{TARGET_DATABASE}.{TARGET_TABLE}",
            "load_mode": self.load_mode,
            "calendar_tz": str(self.amazon_account_timezone),
            "report_id_today": self.report_id_today or "(not set)",
            "report_id_yday": (
                f"{self.report_id_yesterday} (until {self.yesterday_finalize_hours:02d}:00 "
                f"{self.amazon_account_timezone})"
                if self.report_id_yesterday
                else "(not set)"
            ),
            "entity_id": self.amazon_entity_id or "(not set: All accounts)",
            "credentials": "configured" if self.has_credentials else "not configured",
            "auto_login": self.can_auto_login,
            "headless": self.headless,
            "ready_timeout_s": self.report_ready_timeout_seconds,
            "poll_interval_s": self.report_poll_interval_seconds,
            "max_retries": self.max_retries,
        }


def load_settings(env_file: Path | str | None = None, *, override: bool = False) -> Settings:
    """Read, validate and return the runtime configuration.

    Raises:
        ConfigurationError: If a required value is missing or malformed.
    """
    candidate = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=override)
    elif env_file is not None:
        raise ConfigurationError(f"env file not found: {candidate}")

    data_dir = PROJECT_ROOT / "data"

    db_name = _get("DB_NAME", TARGET_DATABASE) or TARGET_DATABASE
    if db_name != TARGET_DATABASE:
        raise ConfigurationError(
            f"DB_NAME is {db_name!r}, but this project only writes to the "
            f"{TARGET_DATABASE} database. Refusing to run against another database."
        )

    ssl_ca_raw = _get("DB_SSL_CA")
    ssl_ca = None
    if ssl_ca_raw:
        ssl_ca = _resolve_path(ssl_ca_raw, PROJECT_ROOT)
        if not ssl_ca.is_file():
            raise ConfigurationError(f"DB_SSL_CA file not found: {ssl_ca}")

    database = DatabaseConfig(
        host=_require("DB_HOST"),
        port=_get_int("DB_PORT", 3306, minimum=1),
        name=db_name,
        user=_require("DB_USER"),
        password=_require("DB_PASSWORD"),
        pool_recycle_seconds=_get_int("DB_POOL_RECYCLE_SECONDS", 1800, minimum=60),
        connect_timeout_seconds=_get_int("DB_CONNECT_TIMEOUT_SECONDS", 30, minimum=1),
        ssl_ca=ssl_ca,
        ssl_verify_cert=_get_bool("DB_SSL_VERIFY_CERT", False),
        ssl_disabled=_get_bool("DB_SSL_DISABLED", False),
    )

    entity_id = _get("AMAZON_ENTITY_ID")
    if entity_id is not None and not ENTITY_ID_PATTERN.match(entity_id):
        raise ConfigurationError(
            f"AMAZON_ENTITY_ID={entity_id!r} is not an entity ID; copy the entityId=ENTITY... "
            "value from the report's URL, or leave it unset for an All-accounts report"
        )
    template = _get(
        "REPORT_HISTORY_URL_TEMPLATE",
        DEFAULT_HISTORY_URL_TEMPLATE if entity_id else LEGACY_HISTORY_URL_TEMPLATE,
    )
    assert template is not None
    parts = urlsplit(template)
    if parts.scheme != "https" or not parts.netloc.startswith("advertising.amazon."):
        raise ConfigurationError(
            "REPORT_HISTORY_URL_TEMPLATE must be an https://advertising.amazon.* URL, "
            f"got {template!r}"
        )
    if "{report_id}" not in template:
        raise ConfigurationError("REPORT_HISTORY_URL_TEMPLATE must contain {report_id}")
    if "{entity_id}" in template and not entity_id:
        raise ConfigurationError("REPORT_HISTORY_URL_TEMPLATE uses {entity_id}; set AMAZON_ENTITY_ID")

    report_id_today = _get_report_id("REPORT_ID_TODAY")
    report_id_yesterday = _get_report_id("REPORT_ID_YESTERDAY")
    if report_id_today and report_id_today == report_id_yesterday:
        raise ConfigurationError(
            "REPORT_ID_TODAY and REPORT_ID_YESTERDAY are the same report. Leave "
            "REPORT_ID_YESTERDAY unset, or point it at a second saved report with period Yesterday."
        )
    if not report_id_today:
        raise ConfigurationError(
            "REPORT_ID_TODAY is not set. Open search_term_Automation in Amazon Ads > Reporting "
            "and copy the reportId=... value from the URL."
        )

    csv_date_format = _get("CSV_DATE_FORMAT")
    if csv_date_format:
        try:
            from datetime import date

            if date(2026, 1, 31).strftime(csv_date_format) == csv_date_format:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(f"CSV_DATE_FORMAT={csv_date_format!r} is not a date format") from exc

    load_mode = (_get("DB_LOAD_MODE", "replace") or "replace").lower()
    if load_mode not in LOAD_MODES:
        raise ConfigurationError(f"DB_LOAD_MODE must be one of {', '.join(LOAD_MODES)}, got {load_mode!r}")

    log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"LOG_LEVEL must be a standard level, got {log_level!r}")

    settings = Settings(
        database=database,
        report_history_url_template=template,
        amazon_entity_id=entity_id,
        report_id_today=report_id_today,
        report_id_yesterday=report_id_yesterday,
        report_name_today=_get("REPORT_NAME_TODAY"),
        report_name_yesterday=_get("REPORT_NAME_YESTERDAY"),
        yesterday_finalize_hours=_get_int("YESTERDAY_FINALIZE_HOURS", 2, minimum=1),
        amazon_email=_get("AMAZON_EMAIL"),
        amazon_password=_get("AMAZON_PASSWORD"),
        auto_login=_get_bool("AMAZON_AUTO_LOGIN", True),
        report_timezone=_get_zone("REPORT_TIMEZONE", "Asia/Kolkata"),
        amazon_account_timezone=_get_zone("AMAZON_ACCOUNT_TIMEZONE", "America/Los_Angeles"),
        max_report_age_days=_get_int("MAX_REPORT_AGE_DAYS", 8, minimum=1),
        csv_date_format=csv_date_format,
        reports_dir=_get_path("REPORTS_DIR", data_dir / "reports"),
        download_dir=_get_path("DOWNLOAD_DIR", data_dir / "downloads"),
        archive_dir=_get_path("ARCHIVE_DIR", data_dir / "archive"),
        failed_dir=_get_path("FAILED_DIR", data_dir / "failed"),
        log_dir=_get_path("LOG_DIR", PROJECT_ROOT / "logs"),
        screenshot_dir=_get_path("SCREENSHOT_DIR", data_dir / "screenshots"),
        session_dir=_get_path("SESSION_DIR", data_dir / "session"),
        lock_file=_get_path("LOCK_FILE", data_dir / "search_term_automation.lock"),
        headless=_get_bool("HEADLESS", True),
        browser_timeout_ms=_get_int("BROWSER_TIMEOUT_MS", 60_000, minimum=1_000),
        download_timeout_ms=_get_int("DOWNLOAD_TIMEOUT_MS", 300_000, minimum=1_000),
        slow_mo_ms=_get_int("SLOW_MO_MS", 0, minimum=0),
        user_agent=_get("BROWSER_USER_AGENT"),
        browser_args=_browser_args(),
        viewport_width=_get_int("VIEWPORT_WIDTH", 1920, minimum=320),
        viewport_height=_get_int("VIEWPORT_HEIGHT", 1080, minimum=320),
        report_ready_timeout_seconds=_get_int("REPORT_READY_TIMEOUT_SECONDS", 900, minimum=30),
        report_poll_interval_seconds=_get_int("REPORT_POLL_INTERVAL_SECONDS", 30, minimum=5),
        max_retries=_get_int("MAX_RETRIES", 2, minimum=1),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 10.0, minimum=0.0),
        retry_backoff_max_seconds=_get_float("RETRY_BACKOFF_MAX_SECONDS", 120.0, minimum=0.0),
        insert_chunk_size=_get_int("DB_INSERT_CHUNK_SIZE", 2000, minimum=1),
        commit_every_rows=_get_int("DB_COMMIT_EVERY_ROWS", 50_000, minimum=0),
        load_mode=load_mode,
        download_latest=_get_bool("DOWNLOAD_LATEST", False),
        retention_days=_get_int("FILE_RETENTION_DAYS", 3, minimum=1),
        log_level=log_level,
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 14, minimum=1),
    )

    if bool(settings.amazon_email) != bool(settings.amazon_password):
        missing = "AMAZON_PASSWORD" if settings.amazon_email else "AMAZON_EMAIL"
        raise ConfigurationError(
            f"{missing} is not set. Automatic sign-in needs both AMAZON_EMAIL and "
            "AMAZON_PASSWORD, or neither (session-only mode)."
        )
    if settings.yesterday_finalize_hours > 24:
        raise ConfigurationError(
            f"YESTERDAY_FINALIZE_HOURS must be 1-24, got {settings.yesterday_finalize_hours}"
        )
    if settings.report_poll_interval_seconds >= settings.report_ready_timeout_seconds:
        raise ConfigurationError(
            "REPORT_POLL_INTERVAL_SECONDS must be smaller than REPORT_READY_TIMEOUT_SECONDS"
        )
    return settings
