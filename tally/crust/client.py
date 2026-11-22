"""The Crustdata HTTP client.

It knows HTTP and nothing about budgets: the wallet decides whether a call may
happen, and this client makes it. What it adds over plain httpx:

* the two required headers;
* a rate limiter per endpoint (Crustdata limits each endpoint per minute);
* safe retries: 429s and 5xx errors are not charged, so they are retried here;
  a timeout might have been charged, so it is raised as `SourceTimeout` for the
  wallet to keep in doubt;
* the cost (`X-Credits-Used`) and request id (`X-Request-Id`) of every call;
* modes: `record` saves every response, and `replay` serves saved responses for
  free (the UI then shows a REPLAY banner). Replay is demo insurance, not data.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from tally.config import Settings
from tally.crust.errors import (
    BadKey, BadRequest, CrustError, EndpointForbidden, OutOfCredits, RateLimited, SourceTimeout,
)
from tally.ledger import canonical_json, sha256

# Default requests per minute, from the handbook and the docs. Load the real
# ones with `GET /account/endpoints` (see `load_rate_limits`).
DEFAULT_RPM: dict[str, int] = {
    "/person/search": 30,
    "/person/enrich": 15,
    "/company/search": 30,
    "/company/identify": 30,
    "/company/enrich": 15,
    "/job/search": 30,
    "/web/search/live": 10,
    "/web/enrich/live": 10,
}
ACCOUNT_RPM = 60  # /account/* calls are free; the usage endpoints share 60 a minute

MAX_429_RETRIES = 5
MAX_5XX_RETRIES = 2
LONGEST_WAIT_SECONDS = 60.0


@dataclass(frozen=True)
class CrustResponse:
    body: Any
    credits: float
    request_id: str | None


class RateLimiter:
    """At most `rpm` requests in any 60-second window, per endpoint."""

    def __init__(self, rpm: dict[str, int], clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.rpm = dict(rpm)
        self._clock = clock
        self._sleep = sleep
        self._sent: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait_turn(self, path: str) -> None:
        limit = self.rpm.get(path, ACCOUNT_RPM if path.startswith("/account/") else 10)
        lock = self._locks.setdefault(path, asyncio.Lock())
        async with lock:
            sent = self._sent.setdefault(path, deque())
            while True:
                now = self._clock()
                while sent and now - sent[0] >= 60.0:
                    sent.popleft()
                if len(sent) < limit:
                    sent.append(now)
                    return
                await self._sleep(60.0 - (now - sent[0]) + 0.05)


class CrustClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, timeout_seconds: float = 30.0):
        if settings.mode in ("live", "record") and not settings.crustdata_api_key:
            raise BadKey("CRUSTDATA_API_KEY is not set (see .env.example)")
        self.settings = settings
        self.mode = settings.mode
        self.limiter = RateLimiter(DEFAULT_RPM, sleep=sleep)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            transport=transport,
            timeout=timeout_seconds,
            headers={
                "authorization": f"Bearer {settings.crustdata_api_key or 'offline'}",
                "x-api-version": settings.api_version,
                "content-type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post(self, path: str, body: dict) -> CrustResponse:
        return await self._send("POST", path, body=body)

    async def get(self, path: str, params: dict | None = None) -> CrustResponse:
        return await self._send("GET", path, params=params)

    async def load_rate_limits(self) -> None:
        """Replace the default limits with this key's own (free call)."""
        endpoints = (await self.get("/account/endpoints")).body.get("endpoints", [])
        for endpoint in endpoints:
            rpm = endpoint.get("effective_rate_limit_rpm")
            if rpm:
                self.limiter.rpm[endpoint["path"]] = int(rpm)

    # ----- internals -----

    async def _send(self, method: str, path: str, body: dict | None = None,
                    params: dict | None = None) -> CrustResponse:
        replay_key = sha256(f"{method}|{path}|{canonical_json(body or params or {})}")
        if self.mode == "replay":
            return self._replayed(replay_key, path)

        rate_limited = server_errors = 0
        while True:
            await self.limiter.wait_turn(path)
            try:
                response = await self._http.request(method, path, json=body, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                raise SourceTimeout(f"{method} {path}: no answer ({type(error).__name__})") from error

            credits = float(response.headers.get("x-credits-used") or 0)
            request_id = response.headers.get("x-request-id")

            if response.status_code == 429 and rate_limited < MAX_429_RETRIES:
                rate_limited += 1
                await self._sleep(_seconds_to_wait(response))
                continue
            if response.status_code >= 500 and server_errors < MAX_5XX_RETRIES:
                server_errors += 1
                await self._sleep(2.0 ** server_errors)
                continue
            if response.is_success:
                result = CrustResponse(body=response.json(), credits=credits, request_id=request_id)
                if self.mode == "record":
                    self._save(replay_key, path, result)
                return result
            raise _error_for(response, credits, request_id)

    def _save(self, replay_key: str, path: str, result: CrustResponse) -> None:
        folder = self.settings.replay_dir
        folder.mkdir(parents=True, exist_ok=True)
        record = {"path": path, "body": result.body, "credits": result.credits,
                  "request_id": result.request_id, "recorded_at": time.time()}
        (folder / f"{replay_key}.json").write_text(json.dumps(record), encoding="utf-8")

    def _replayed(self, replay_key: str, path: str) -> CrustResponse:
        file = Path(self.settings.replay_dir) / f"{replay_key}.json"
        if not file.exists():
            raise CrustError(f"REPLAY: no saved response for {path}; record it first", status=None)
        record = json.loads(file.read_text(encoding="utf-8"))
        # A replay costs nothing, and says so in its request id.
        return CrustResponse(body=record["body"], credits=0.0, request_id=f"replay:{record['request_id']}")


def _seconds_to_wait(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("x-ratelimit-reset", "5"))
    except ValueError:
        seconds = 5.0
    return min(max(seconds, 1.0), LONGEST_WAIT_SECONDS)


def _error_for(response: httpx.Response, credits: float, request_id: str | None) -> CrustError:
    """Turn an error response into the error class the caller acts on."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("error", payload) if isinstance(payload, dict) else {}
    message = detail.get("message") if isinstance(detail, dict) else None
    message = f"{response.status_code} {response.request.url.path}: {message or response.reason_phrase}"
    error_class = {401: BadKey, 402: OutOfCredits, 403: EndpointForbidden, 400: BadRequest,
                   429: RateLimited}.get(response.status_code, CrustError)
    return error_class(message, status=response.status_code, credits=credits, request_id=request_id)
