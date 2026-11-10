"""Interactive two-step sign-in against mock pages (real Chromium).

Models Amazon's newer flow seen 2026-11-10 at /ap/mfa/new-otp: first choose
where to send the code ("Send OTP"), then type it.  The mock is not Amazon's
markup -- it only uses the ids/names the selectors rely on.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.auth import sign_in_interactive
from app.browser import BrowserSession
from app.exceptions import AuthenticationError

SEND_PAGE = """<!doctype html><html><body>
<h1>Two-Step Verification</h1>
<form id="send">
  <label><input type="radio" name="otpDeviceContext" value="sms" checked> Text me at my number ending in 42</label>
  <label><input type="radio" name="otpDeviceContext" value="voice"> Call me at my number ending in 42</label>
  <input type="submit" id="auth-send-code" value="Send OTP">
</form>
<script>
document.getElementById('send').addEventListener('submit', e => {
  e.preventDefault();
  const chosen = document.querySelector("input[name='otpDeviceContext']:checked").value;
  document.body.innerHTML = `
    <h1>Two-Step Verification</h1><p>A code was sent via ${chosen}.</p>
    <input id="auth-mfa-otpcode" name="otpCode" type="tel">
    <label><input id="auth-mfa-remember-device" name="rememberDevice" type="checkbox"> Don't require code</label>
    <input id="auth-signin-button" type="submit" value="Sign in">`;
  window.sentVia = chosen;
  document.getElementById('auth-signin-button').addEventListener('click', () => {
    window.result = {code: document.getElementById('auth-mfa-otpcode').value,
                     remember: document.getElementById('auth-mfa-remember-device').checked, via: chosen};
    document.body.innerHTML = '<h1>Report history</h1>';
  });
});
</script></body></html>"""


def _no_credentials(settings):
    return dataclasses.replace(settings, amazon_email=None, amazon_password=None)


def test_new_otp_page_sends_the_code_to_the_chosen_device_then_asks_for_it(settings):
    answers = iter(["2", "123 456"])
    shown: list[str] = []
    with BrowserSession(settings, headless=True) as session:
        session.page.set_content(SEND_PAGE)
        sign_in_interactive(session, _no_credentials(settings), prompt=lambda _: next(answers), announce=shown.append)
        result = session.page.evaluate("window.result")
    assert result == {"code": "123456", "remember": True, "via": "voice"}
    assert any("1. Text me at my number ending in 42" in line for line in shown)
    assert any("Code accepted" in line for line in shown)


def test_an_unrecognised_challenge_page_stops_instead_of_navigating_away(settings):
    with BrowserSession(settings, headless=True) as session:
        session.page.set_content("<h1>Two-Step Verification</h1><p>Approve the sign-in in the Amazon app.</p>")
        with pytest.raises(AuthenticationError, match="does not recognise"):
            sign_in_interactive(session, _no_credentials(settings), prompt=lambda _: "", announce=lambda _: None)
        assert "Approve the sign-in" in session.page.inner_text("body")
