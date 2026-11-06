"""Single-instance execution lock.

``flock`` on a dedicated file: the kernel releases it if the process is killed,
so a crashed run cannot wedge the schedule the way a stale PID file would.

This is the second line of defence; the cron entry also wraps the run in
``flock(1)`` on a *different* file (an inherited lock on the same file would
make this one refuse to start).  On Windows -- local development only --
``msvcrt`` provides the same non-blocking exclusive lock.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .exceptions import LockError
from .logging_config import get_logger

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

logger = get_logger("lock")


def _acquire(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _release(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def run_lock(lock_file: Path, *, enabled: bool = True) -> Iterator[None]:
    """Hold an exclusive lock for the duration of the block.

    Raises:
        LockError: If another run already holds the lock.
    """
    if not enabled:
        yield
        return

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+")
    try:
        try:
            _acquire(handle)
        except OSError as exc:
            raise LockError(
                f"Another search term automation run is in progress (lock {lock_file}). "
                "Refusing to start a second run."
            ) from exc

        if fcntl is not None:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        logger.debug("Acquired run lock %s", lock_file)
        try:
            yield
        finally:
            _release(handle)
            logger.debug("Released run lock %s", lock_file)
    finally:
        handle.close()
