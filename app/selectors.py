"""Selector catalogue and resolution helpers.

Amazon ships DOM changes to Campaign Manager without notice, so every element
is addressed by an ordered list of *candidate* selectors loaded from
``config/selectors.json``.  Fixing a broken run is then a config edit, not a
code change and redeploy.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from time import monotonic, sleep

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .config import PROJECT_ROOT
from .exceptions import ConfigurationError
from .logging_config import get_logger

logger = get_logger("selectors")

DEFAULT_SELECTOR_FILE = PROJECT_ROOT / "config" / "selectors.json"

#: How long to wait on a single candidate before moving to the next one.  Short
#: by design: the list is a search, not a wait.
CANDIDATE_TIMEOUT_MS = 2_500

#: Floor on how long one pass over the candidate list may take before the next
#: pass starts.  Guards against spinning when every probe returns instantly.
_MIN_PASS_SECONDS = 0.1


@lru_cache(maxsize=4)
def load_selectors(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Load the selector catalogue, ignoring ``_``-prefixed comment keys."""
    source = path or DEFAULT_SELECTOR_FILE
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Selector file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Selector file {source} is not valid JSON: {exc}") from exc

    catalogue: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigurationError(
                f"Selector entry {key!r} in {source} must be a list of selector strings"
            )
        catalogue[key] = tuple(value)
    return catalogue


def candidates(name: str, path: Path | None = None) -> tuple[str, ...]:
    """Selector candidates registered under ``name``."""
    catalogue = load_selectors(path)
    try:
        return catalogue[name]
    except KeyError as exc:
        raise ConfigurationError(f"No selector candidates configured for {name!r}") from exc


def find_first(
    page: Page,
    name: str,
    *,
    timeout_ms: int = CANDIDATE_TIMEOUT_MS,
    path: Path | None = None,
) -> Locator | None:
    """Return the first candidate for ``name`` that becomes visible, else None.

    ``timeout_ms`` is the budget for the **whole search**, not for each
    candidate.  Each one gets a short probe and the list is cycled until the
    budget runs out, so a five-candidate list with a 60 s budget still answers
    in seconds when the page is ready -- rather than spending 60 s per
    candidate and appearing to hang for five minutes.
    """
    deadline = monotonic() + timeout_ms / 1000
    selectors = candidates(name, path)
    attempted: set[str] = set()

    while True:
        remaining_ms = int((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        probe_ms = max(50, min(CANDIDATE_TIMEOUT_MS, remaining_ms))
        pass_started = monotonic()

        for selector in selectors:
            if monotonic() >= deadline and attempted:
                break
            try:
                # Locator construction is inside the try on purpose: a
                # malformed selector in selectors.json raises here, and one bad
                # entry must not take the whole candidate list down with it.
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=probe_ms)
            except PlaywrightTimeoutError:
                attempted.add(selector)
                continue
            except Exception as exc:  # a malformed selector must not abort the search
                logger.debug("Selector %r for %s raised %s", selector, name, exc)
                attempted.add(selector)
                continue
            logger.debug("Matched %s via %r", name, selector)
            return locator

        # A pass normally costs probe_ms per candidate because wait_for blocks.
        # If it came back instantly -- every selector failing to even construct,
        # say -- pause before retrying so the budget is not burned in a hot loop.
        if monotonic() - pass_started < _MIN_PASS_SECONDS:
            sleep(min(_MIN_PASS_SECONDS, max(0.0, deadline - monotonic())))

    logger.debug(
        "No candidate for %s became visible within %d ms (tried %d selector(s))",
        name,
        timeout_ms,
        len(selectors),
    )
    return None


def is_present(
    page: Page,
    name: str,
    *,
    timeout_ms: int = 1_000,
    path: Path | None = None,
) -> bool:
    """Whether any candidate for ``name`` is currently visible."""
    return find_first(page, name, timeout_ms=timeout_ms, path=path) is not None
