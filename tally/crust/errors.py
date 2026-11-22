"""Errors from Crustdata, one class per thing the caller should do differently."""

from __future__ import annotations


class CrustError(Exception):
    """Crustdata answered with an error. `credits` is what it charged (normally 0)."""

    def __init__(self, message: str, status: int | None = None, credits: float = 0.0,
                 request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.credits = credits
        self.request_id = request_id


class BadKey(CrustError):
    """401: the API key is missing or wrong. Stop and fix the key."""


class OutOfCredits(CrustError):
    """402: no credits left. Retrying won't help; stop the run."""


class EndpointForbidden(CrustError):
    """403: this key can't call this endpoint (or field). The planner drops it."""


class BadRequest(CrustError):
    """400: the request itself is wrong, e.g. an unknown filter field."""


class RateLimited(CrustError):
    """429 that persisted through every retry."""


class SourceTimeout(Exception):
    """No answer at all (timeout or network failure). We can't know if it was charged.

    Not a CrustError on purpose: there is no response, so the wallet keeps the
    call in doubt instead of treating it as a free error.
    """
