"""Automated Amazon sign-in.

This workflow prefers a **reused session**: Chromium's persistent profile keeps the
advertiser signed in, and most runs never touch a login form at all.  This
module is the fallback for when that session is missing or has expired --
it fills in the configured credentials so a scheduled run can recover without
a human.

What :func:`sign_in` -- the unattended path -- will not do:

* It does not read, solve, or submit a CAPTCHA.
* It does not generate, guess or replay an OTP / MFA code.
* It does not disguise the browser or defeat bot detection.

When Amazon answers with any of those, sign-in stops with
:class:`AuthenticationError` and the run ends.  Those challenges exist to
confirm a human is present, and the correct response is to fetch one --
``python run.py --login``.

:func:`sign_in_interactive` is that response, for a server with no display.
It is the same form-filling, plus one thing: if Amazon asks for a two-step
code, it *asks the person running the command* to type it.  The boundary above
is unchanged -- the code still comes from the human and their registered
device, and a scheduled run can never reach this function.
"""

from __future__ import annotations

from collections.abc import Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .browser import BrowserSession, safe_url
from .config import Settings
from .exceptions import AuthenticationError
from .logging_config import get_logger
from .selectors import candidates, find_first, is_present

logger = get_logger("auth")

#: How long to wait for the page to settle after submitting a form.
_SETTLE_MS = 15_000

#: How many times a person may mistype the two-step code before giving up.
#: Amazon invalidates the code after a few tries anyway; stopping first keeps
#: the automation from being the thing that locks the account.
_MAX_OTP_ATTEMPTS = 3


def sign_in(session: BrowserSession, settings: Settings) -> None:
    """Complete the Amazon sign-in form using the configured credentials.

    The caller is responsible for having navigated to a page that is showing
    sign-in.  On success the browser is left on whatever Amazon redirected to.

    Raises:
        AuthenticationError: If credentials are not configured, are rejected,
            or Amazon presents a challenge that requires a person.  Always
            non-transient: this is never retried.
    """
    if not settings.has_credentials:
        raise AuthenticationError(
            "Amazon session is missing or expired and no credentials are configured. "
            "Either set AMAZON_EMAIL and AMAZON_PASSWORD, or re-establish the session "
            "manually with: python run.py --login"
        )

    page = session.page
    logger.info("Signing in to Amazon as %s", _mask_email(settings.amazon_email or ""))

    _assert_no_challenge(session, stage="before sign-in")
    _submit_email(session, page, settings)
    _submit_password(session, page, settings)
    _assert_credentials_accepted(session, page)
    _assert_no_challenge(session, stage="after sign-in")

    logger.info("Sign-in submitted; now at %s", safe_url(page.url))


def sign_in_interactive(
    session: BrowserSession,
    settings: Settings,
    *,
    prompt: Callable[[str], str] = input,
    announce: Callable[[str], None] = print,
) -> None:
    """Sign in with a person at the keyboard, reading the OTP from the terminal.

    This is the ``--login`` path for a host with no display.  It differs from
    :func:`sign_in` in exactly one respect: when Amazon asks for a two-step
    verification code, the code is *read from the person running the command*
    instead of being treated as a dead end.

    That keeps the control doing its job.  The code still has to come from the
    device Amazon sent it to, and nothing here generates, guesses or replays
    one -- the only change is that the human types it into a terminal rather
    than into a browser window.  Unattended runs are untouched: :func:`sign_in`
    still refuses every challenge, so a scheduled job can never reach this.

    An image CAPTCHA still stops the run.  It cannot be answered by someone who
    cannot see it, so the caller is pointed at a display-based login instead.

    Raises:
        AuthenticationError: Credentials rejected, a CAPTCHA is in the way, the
            code was mistyped too many times, or the terminal supplied no input.
    """
    page = session.page

    if settings.has_credentials:
        logger.info("Signing in to Amazon as %s", _mask_email(settings.amazon_email or ""))
    else:
        logger.info("No credentials configured; expecting an already-filled form")

    # Amazon's steps come in varying order: email -> password -> "send code" ->
    # code, and after the code it often asks for the password once more (and
    # sometimes for another code).  Answer whatever page is showing, a few
    # rounds at most, until none of those forms is left.
    for _ in range(_MAX_SIGNIN_ROUNDS):
        if settings.has_credentials and _shows_credentials_form(page):
            _submit_email(session, page, settings)
            _submit_password(session, page, settings)
            _assert_credentials_accepted(session, page)
            continue
        if _shows_two_step(page):
            _answer_otp_if_asked(session, prompt=prompt, announce=announce)
            continue
        break
    else:
        details = session.capture_failure("signin-too-many-steps")
        raise AuthenticationError(
            f"Amazon kept asking for more sign-in steps ({_page_heading(page)}). "
            f"Screenshot: {details.get('screenshot', '(none)')}. Run 'python run.py --login' again."
        )

    if _is_sign_in_page(page):
        details = session.capture_failure("signin-restarted")
        raise AuthenticationError(
            "After the verification code, Amazon went back to its sign-in page "
            f"({_page_heading(page)}). This usually means the sign-in timed out: run "
            "'python run.py --login' again and enter the code within a minute or two "
            f"(an authenticator-app code changes every 30 seconds). Screenshot: {details.get('screenshot', '(none)')}"
        )
    logger.info("Interactive sign-in finished; now at %s", safe_url(page.url))


#: Upper bound on email/password/code pages answered in one interactive login.
_MAX_SIGNIN_ROUNDS = 6


def _shows_credentials_form(page: Page) -> bool:
    """An email or password box is showing (first sign-in, or the re-prompt after the code)."""
    return find_first(page, "signin_password_input", timeout_ms=2_000) is not None or (
        find_first(page, "signin_email_input", timeout_ms=1_000) is not None
    )


def _shows_two_step(page: Page) -> bool:
    """Amazon is on a two-step / "confirm it's you" / CAPTCHA page."""
    return (
        _is_challenge_url(page.url)
        or find_first(page, "otp_send_code_button", timeout_ms=1_500) is not None
        or find_first(page, "otp_input", timeout_ms=1_500) is not None
        or is_present(page, "security_challenge_marker", timeout_ms=1_000)
    )


def _is_sign_in_page(page: Page) -> bool:
    path = page.url.split("?", 1)[0].lower()
    return "/ap/signin" in path or is_present(page, "sign_in_marker", timeout_ms=1_500)


def _answer_otp_if_asked(
    session: BrowserSession,
    *,
    prompt: Callable[[str], str],
    announce: Callable[[str], None],
) -> None:
    """Fill Amazon's two-step code from the terminal, if it is asking for one."""
    page = session.page

    if is_present(page, "captcha_marker", timeout_ms=1_500):
        details = session.capture_failure("signin-captcha")
        shot = details.get("screenshot", "(no screenshot)")
        raise AuthenticationError(
            "Amazon is showing an image CAPTCHA, which cannot be answered from a "
            f"terminal. A screenshot was saved to {shot} -- if you can read it, "
            "re-run on a machine with a display, or copy the saved Chromium "
            "profile from one. CAPTCHAs are deliberately not automated."
        )

    _send_otp_if_asked(session, prompt=prompt, announce=announce)

    field = find_first(page, "otp_input", timeout_ms=10_000)
    if field is None:
        if _is_challenge_url(page.url) or is_present(page, "security_challenge_marker", timeout_ms=5_000):
            # Amazon is still asking for something, but not in a form we know.
            # Stop here: navigating away would throw the page (and the code
            # Amazon may already have sent) away and only show the sign-in form.
            details = session.capture_failure("otp-page-not-recognised")
            shot = details.get("screenshot", "(no screenshot)")
            raise AuthenticationError(
                f"Amazon is asking for two-step verification on a page this job does not "
                f"recognise ({safe_url(page.url)}: {_page_heading(page)}). Screenshot: {shot}. "
                "Send it to the developer so config/selectors.json can be updated."
            )
        logger.debug("No OTP field; Amazon did not ask for two-step verification")
        return

    announce("")
    announce("=" * 72)
    announce("Amazon is asking for a two-step verification code.")
    announce(_otp_destination(page))
    announce("Enter it below. It is NOT stored or logged.")
    announce("=" * 72)

    for attempt in range(1, _MAX_OTP_ATTEMPTS + 1):
        code = _read_otp(prompt, announce, attempt)
        field.fill(code)

        # This checkbox is the whole point of doing the login once: it tells
        # Amazon to trust this Chromium profile, so later scheduled runs
        # reusing SESSION_DIR stop being asked for a code at all.
        remember = find_first(page, "otp_remember_device", timeout_ms=1_500)
        if remember is not None:
            try:
                remember.check()
                logger.info("Ticked 'don't require a code on this browser'")
            except Exception as exc:  # pragma: no cover - cosmetic
                logger.debug("Could not tick 'remember this device': %s", exc)

        submit = find_first(page, "otp_submit_button", timeout_ms=4_000)
        if submit is None:
            session.capture_failure("otp-submit-missing")
            raise AuthenticationError(
                "The two-step code box was there but its submit button was not "
                "(config/selectors.json 'otp_submit_button')."
            )
        submit.click()
        _settle(page)

        field = find_first(page, "otp_input", timeout_ms=3_000)
        if field is None:
            announce("  Code accepted.")
            logger.info("Two-step verification accepted")
            return

        # Still on the OTP form: the code was wrong, expired, or already used.
        announce(f"  Amazon did not accept that code.{_otp_error_suffix(page)}")
        if attempt < _MAX_OTP_ATTEMPTS:
            announce("  Check for a newer code -- Amazon invalidates the previous one.")

    session.capture_failure("otp-rejected")
    raise AuthenticationError(
        f"The two-step code was not accepted after {_MAX_OTP_ATTEMPTS} attempts. "
        "Stopping rather than risking a lock on the account. Request a fresh code "
        "and run 'python run.py --login' again."
    )


def _send_otp_if_asked(
    session: BrowserSession,
    *,
    prompt: Callable[[str], str],
    announce: Callable[[str], None],
) -> None:
    """Amazon's newer two-step page first asks where to send the code.

    It lists the registered devices (phone, email, authenticator app) as radio
    buttons with a "Send OTP" button, and only then shows the code box.  The
    person picks the device; nothing is sent without that choice being theirs
    (a single option is used as-is).
    """
    page = session.page
    send = find_first(page, "otp_send_code_button", timeout_ms=4_000)
    if send is None:
        return

    options = []
    for selector in candidates("otp_device_option"):
        locator = page.locator(selector)
        if locator.count():
            options = [locator.nth(i) for i in range(locator.count())]
            break

    announce("")
    announce("=" * 72)
    announce("Amazon wants to send a two-step verification code.")
    if len(options) > 1:
        labels = []
        for option in options:
            try:
                text = option.evaluate(_OPTION_LABEL_JS)
            except Exception:  # pragma: no cover - defensive
                text = ""
            labels.append(" ".join(text.split())[:100] or "(unnamed option)")
        for number, label in enumerate(labels, start=1):
            announce(f"  {number}. {label}")
        choice = _read_choice(prompt, announce, len(labels))
        try:
            options[choice - 1].check()
        except Exception as exc:  # pragma: no cover - the label may hide the input
            logger.debug("Could not tick option %d directly (%s); clicking it", choice, exc)
            options[choice - 1].click(force=True)
        announce(f"  Sending the code to option {choice}...")
    else:
        announce("  Sending the code...")
    announce("=" * 72)

    send.click()
    _settle(page)
    logger.info("Requested a two-step verification code (now at %s)", safe_url(page.url))


#: The text belonging to ONE radio option: its own <label for=...>, else the
#: text right after it, else the nearest label/parent.  (Taking the parent first
#: gave every authenticator app the same shared heading.)
_OPTION_LABEL_JS = """e => {
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
  if (e.id) {
    const own = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
    if (own && clean(own.innerText)) return clean(own.innerText);
  }
  let parts = [];
  for (let n = e.nextSibling; n && parts.join(' ').length < 120; n = n.nextSibling) {
    if (n.nodeType === 1 && n.matches && n.matches("input[type='radio']")) break;
    parts.push(clean(n.textContent));
  }
  const after = clean(parts.join(' '));
  if (after) return after;
  return clean((e.closest('label') || e.parentElement || e).innerText);
}"""


def _read_choice(prompt: Callable[[str], str], announce: Callable[[str], None], count: int) -> int:
    """Which device to send the code to; Enter keeps option 1 (Amazon's default)."""
    while True:
        try:
            raw = prompt(f"  Where should Amazon send the code? [1-{count}, Enter = 1]: ").strip()
        except EOFError as exc:
            raise AuthenticationError(
                "No terminal input is available to choose where the code is sent. "
                "Run 'python run.py --login' from an interactive shell."
            ) from exc
        if not raw:
            return 1
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw)
        announce(f"  Type a number from 1 to {count}.")


def _page_heading(page: Page) -> str:
    """The first meaningful line of the page, for error messages."""
    try:
        text = page.inner_text("body", timeout=2_000)
    except Exception:  # pragma: no cover - defensive
        return "(page text unavailable)"
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 3]
    return " | ".join(lines[:3])[:200] or "(empty page)"


def _read_otp(
    prompt: Callable[[str], str],
    announce: Callable[[str], None],
    attempt: int,
) -> str:
    """Read a verification code from the terminal, tolerating spaces and dashes."""
    while True:
        try:
            raw = prompt(f"  Verification code (attempt {attempt}/{_MAX_OTP_ATTEMPTS}): ")
        except EOFError as exc:
            raise AuthenticationError(
                "No terminal input is available, so the two-step code cannot be "
                "asked for. Run 'python run.py --login' from an interactive shell."
            ) from exc

        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            return digits
        announce("  That was empty. Type the digits Amazon sent, e.g. 123456.")


def _otp_destination(page: Page) -> str:
    """Amazon's own one-line description of where it sent the code."""
    try:
        text = page.inner_text("body", timeout=2_000)
    except Exception:  # pragma: no cover - defensive
        return "Check the device registered on the account."
    for line in (ln.strip() for ln in text.splitlines()):
        if "code" in line.lower() and ("sent" in line.lower() or "ending in" in line.lower()):
            return line[:200]
    return "Check the device registered on the account."


def _otp_error_suffix(page: Page) -> str:
    """Amazon's inline complaint about the code, if it printed one."""
    banner = find_first(page, "signin_error_message", timeout_ms=1_500)
    if banner is None:
        return ""
    try:
        return f" {banner.inner_text(timeout=1_500).strip()[:200]}"
    except Exception:  # pragma: no cover - defensive
        return ""


def _submit_email(session: BrowserSession, page: Page, settings: Settings) -> None:
    """Fill the email field and continue, if Amazon is asking for it.

    Amazon serves two layouts: email then password on separate pages, or both
    together.  A missing email field means we are already on the password step.
    """
    field = find_first(page, "signin_email_input", timeout_ms=5_000)
    if field is None:
        logger.debug("No email field on this page; assuming the password step")
        return

    # fill() sets the value through the DOM: the secret is never typed into a
    # log, a screenshot of a focused field, or a Playwright trace as keystrokes.
    email = settings.amazon_email or ""
    field.fill(email)
    if field.input_value() != email:
        # Seen on the live Amazon Ads page (2026-10-22): the box ended up with
        # the address plus leftover text, and Amazon answered "cannot find an
        # account".  Clear it and fill once more before submitting.
        logger.warning("Email field did not hold exactly the configured address; filling it again")
        field.fill("")
        field.fill(email)
        if field.input_value() != email:
            session.capture_failure("signin-email-mismatch")
            raise AuthenticationError(
                "The sign-in email box would not hold the configured AMAZON_EMAIL exactly. "
                "Run 'python run.py --login --headed' and type it in the browser."
            )

    remember = find_first(page, "signin_remember_me", timeout_ms=1_500)
    if remember is not None:
        try:
            remember.check()
            logger.debug("Enabled 'keep me signed in' to extend the session")
        except Exception as exc:  # pragma: no cover - cosmetic
            logger.debug("Could not tick 'keep me signed in': %s", exc)

    continue_button = find_first(page, "signin_continue_button", timeout_ms=5_000)
    if continue_button is None:
        logger.debug("No continue button; the form is probably single-page")
        return
    continue_button.click()
    _settle(page)
    _assert_credentials_accepted(session, page)


def _submit_password(session: BrowserSession, page: Page, settings: Settings) -> None:
    """Fill the password field and submit."""
    field = find_first(page, "signin_password_input", timeout_ms=10_000)
    if field is None:
        session.capture_failure("signin-password-field-missing")
        raise AuthenticationError(
            "Reached sign-in but the password field never appeared. Amazon may have "
            "changed the form (config/selectors.json 'signin_password_input'), or it is "
            "asking for something only a person can supply. Run: python run.py --login"
        )
    field.fill(settings.amazon_password or "")

    remember = find_first(page, "signin_remember_me", timeout_ms=1_500)
    if remember is not None:
        try:
            remember.check()
        except Exception:  # pragma: no cover - cosmetic
            pass

    submit = find_first(page, "signin_submit_button", timeout_ms=5_000)
    if submit is None:
        session.capture_failure("signin-submit-missing")
        raise AuthenticationError(
            "Could not find the sign-in submit button "
            "(config/selectors.json 'signin_submit_button')."
        )
    submit.click()
    _settle(page)


def _assert_credentials_accepted(session: BrowserSession, page: Page) -> None:
    """Stop if Amazon rejected the email or password.

    Wrong credentials are never a transient failure.  Repeating them locks the
    account, so this raises immediately and the retry layer does not re-enter.
    """
    banner = find_first(page, "signin_error_message", timeout_ms=2_000)
    if banner is None:
        return

    try:
        message = banner.inner_text(timeout=2_000).strip()
    except Exception:  # pragma: no cover - defensive
        message = "(could not read the error text)"

    session.capture_failure("signin-rejected")
    raise AuthenticationError(
        f"Amazon rejected the sign-in: {message[:300]} "
        "Check AMAZON_EMAIL and AMAZON_PASSWORD. It will not be retried, to avoid "
        "locking the account."
    )


def _assert_no_challenge(session: BrowserSession, *, stage: str) -> None:
    """Stop if Amazon is asking for a CAPTCHA, OTP or MFA code.

    This is the boundary the module will not cross.  Solving these would defeat
    a control whose entire purpose is to confirm a person is present.
    """
    # Amazon redirects to /ap/mfa or /ap/cvf before the challenge form has
    # rendered, so the URL is checked too -- otherwise a two-step prompt slips
    # through here and is later misreported as rejected credentials.
    if not _is_challenge_url(session.page.url) and not is_present(
        session.page, "security_challenge_marker", timeout_ms=2_000
    ):
        return

    session.capture_failure(f"security-challenge-{stage.replace(' ', '-')}")
    raise AuthenticationError(
        f"Amazon presented a security challenge (CAPTCHA/OTP/MFA) {stage}. "
        "These are not answered automatically. Complete it once by hand with "
        "'python run.py --login' -- the session is then saved and reused, so "
        "scheduled runs stop hitting the login form."
    )


#: Amazon's challenge pages: two-step verification and "confirm it's you".
_CHALLENGE_PATHS = ("/ap/mfa", "/ap/cvf")


def _is_challenge_url(url: str) -> bool:
    """Whether ``url`` is one of Amazon's challenge pages (query ignored)."""
    path = url.split("?", 1)[0].lower()
    return any(marker in path for marker in _CHALLENGE_PATHS)


def _settle(page: Page) -> None:
    """Wait for the navigation a form submission triggers."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=_SETTLE_MS)
    except PlaywrightTimeoutError:
        logger.debug("Page did not report domcontentloaded within %d ms", _SETTLE_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
    except PlaywrightTimeoutError:
        logger.debug("Page did not reach networkidle; continuing")


def _mask_email(email: str) -> str:
    """``someone@example.com`` -> ``s*****e@example.com``.

    Enough to confirm which account was used, without writing the full address
    into a log that may be shipped off the host.
    """
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"
