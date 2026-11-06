"""The saved Amazon session: backup to session_state.json and restore into an empty profile (real Chromium)."""

from __future__ import annotations

import dataclasses
import json
import shutil

from app.browser import BrowserSession

COOKIE = {"name": "session-token", "value": "abc", "domain": ".amazon.de", "path": "/",
          "expires": 4_102_444_800, "httpOnly": True, "secure": True, "sameSite": "Lax"}


def test_session_backup_is_restored_into_a_wiped_profile(settings):
    with BrowserSession(settings, headless=True) as session:
        assert session.save_state() is False  # no Amazon cookies yet: nothing written
        assert not session.state_file.exists()
        session.context.add_cookies([COOKIE])
        assert session.save_state() is True
    state_file = settings.session_dir.with_name("session_state.json")
    assert session.state_file == state_file
    assert [c["name"] for c in json.loads(state_file.read_text())["cookies"]] == ["session-token"]

    shutil.rmtree(settings.session_dir)  # profile lost
    with BrowserSession(settings, headless=True) as session:
        assert [c["value"] for c in session.context.cookies("https://advertising.amazon.de")] == ["abc"]


def test_profile_cookies_win_over_an_older_backup(settings):
    with BrowserSession(settings, headless=True) as session:
        session.context.add_cookies([COOKIE])
        session.save_state()
        session.context.add_cookies([{**COOKIE, "value": "newer"}])  # Amazon rotated it
    with BrowserSession(dataclasses.replace(settings), headless=True) as session:
        assert [c["value"] for c in session.context.cookies("https://advertising.amazon.de")] == ["newer"]
