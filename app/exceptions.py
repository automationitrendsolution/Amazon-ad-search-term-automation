"""Exception hierarchy.

Every failure mode the pipeline can hit has its own type so that callers can
decide independently whether to retry, alert, or abort.  ``transient`` marks the
errors that are safe to retry; authentication/CAPTCHA/MFA failures are never
transient and must never be retried automatically.
"""

from __future__ import annotations


class AdFlowError(Exception):
    """Base class for every failure."""

    #: Whether retrying the same operation could plausibly succeed.
    transient: bool = False
    #: Process exit code used when this error reaches the top level.
    exit_code: int = 1


class ConfigurationError(AdFlowError):
    """Missing or invalid environment configuration."""

    exit_code = 2


class AuthenticationError(AdFlowError):
    """Amazon session is missing, expired, or blocked by a security challenge.

    Never retried: repeated automated attempts against sign-in, CAPTCHA, OTP or
    MFA are exactly what Amazon's protections exist to stop.  A human must
    re-establish the session with ``python run.py --login``.
    """

    exit_code = 3


class NavigationError(AdFlowError):
    """The reporting page did not reach the expected state."""

    transient = True
    exit_code = 4


class DownloadError(AdFlowError):
    """The report download did not start, did not finish, or produced no file."""

    transient = True
    exit_code = 6


class CSVValidationError(AdFlowError):
    """Downloaded file is not a usable Search term report."""

    exit_code = 7


class DataTransformationError(AdFlowError):
    """Report could not be transformed into the destination layout."""

    exit_code = 8


class DatabaseError(AdFlowError):
    """MySQL connectivity, schema or transaction failure.

    ``transient`` is set per instance by :mod:`app.database`: a dropped
    connection or a lost lock race is retried, a schema or data error is not.
    """

    exit_code = 9


class LockError(AdFlowError):
    """Another run already holds the execution lock."""

    exit_code = 11


class ReportIdError(AdFlowError):
    """A report ID is malformed, or Amazon does not recognise it (or the page
    shows another report's name).

    Not transient: guessing or reusing another date's report would load the
    wrong day.
    """

    exit_code = 12


class ReportNotReadyError(AdFlowError):
    """The report run never left the ``-`` (pending) state, or Amazon marked it
    as failed.

    Transient: the next attempt runs the report again.
    """

    transient = True
    exit_code = 13


class DateMismatchError(AdFlowError):
    """The report covers a different date than the one expected (or several)."""

    exit_code = 14


class ReportsNotSavedError(AdFlowError):
    """--load / --save-reports could not load every selected report (details in the summary)."""

    exit_code = 16
