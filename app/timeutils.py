"""Timezone-aware time handling.

Every timestamp is explicit about its zone.  The server's local time is never
used, so the same code picks the same Amazon dates on a UTC cron host, a
laptop in IST, and a container with no ``/etc/localtime``.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import UTC

__all__ = ["UTC", "now_utc", "now_in"]


def now_utc() -> datetime:
    """Current instant, in UTC."""
    return datetime.now(tz=UTC)


def now_in(tz: ZoneInfo, *, reference: datetime | None = None) -> datetime:
    """Convert ``reference`` (default: now) into ``tz``."""
    return (reference or now_utc()).astimezone(tz)
