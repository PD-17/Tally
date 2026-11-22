"""After a crash: find what was left open, learn what it cost, and prove the ledger matches Crustdata.

A process killed mid-call leaves its purchase `reserved`: the request may or
may not have reached Crustdata. On restart:

1. every open reservation becomes `in_doubt`, counted at its reserved amount;
2. Crustdata's usage records (free) list every charged request with its id.
   A charge whose request id isn't in our ledger belongs to an in-doubt call
   on the same endpoint, so that call is resolved at the real amount; if no
   such charge exists, it was never charged and resolves at 0;
3. the ledger total for the run is compared with the drop in the account
   balance since the run started: the green check on the audit screen.

The balance check assumes nobody else spends on the same key during the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from tally.crust.client import CrustClient
from tally.ledger import Ledger


@dataclass(frozen=True)
class BalanceCheck:
    ledger_credits: float
    balance_drop: float | None  # None when the starting balance wasn't recorded

    @property
    def matches(self) -> bool | None:
        if self.balance_drop is None:
            return None
        return abs(self.ledger_credits - self.balance_drop) < 0.005


def reopen_after_crash(ledger: Ledger, run_id: str) -> list[str]:
    """Mark this run's open reservations as in doubt. Returns their call keys."""
    keys = []
    for state in ledger.open_reservations():
        if state.run_id == run_id:
            ledger.mark_in_doubt(state.call_key, "left open when the process stopped")
            keys.append(state.call_key)
    return keys


async def settle_in_doubt(ledger: Ledger, client: CrustClient, run_id: str, run_started_at: float) -> list[str]:
    """Resolve in-doubt calls from Crustdata's usage records. Returns one line per call resolved."""
    doubtful = [s for s in ledger.states(run_id) if s.event == "in_doubt"]
    if not doubtful:
        return []
    started = datetime.fromtimestamp(run_started_at, tz=timezone.utc).isoformat()
    events = (await client.get("/account/usage/events", {"start": started, "limit": 500})).body.get("events", [])
    known_ids = {s.request_id for s in ledger.states() if s.request_id}
    unclaimed = [e for e in events if e.get("event_id") not in known_ids and float(e.get("credits_used") or 0) > 0]

    lines = []
    for state in doubtful:
        match = next((e for e in unclaimed if e.get("endpoint") == state.endpoint), None)
        if match:
            unclaimed.remove(match)
            credits = float(match["credits_used"])
            ledger.resolve(state.call_key, credits, "usage records show this charge", request_id=match["event_id"])
            lines.append(f"{state.endpoint}: charged {credits:g} (request {match['event_id']})")
        else:
            ledger.resolve(state.call_key, 0.0, "no charge in usage records")
            lines.append(f"{state.endpoint}: never charged")
    return lines


async def account_balance(client: CrustClient) -> float | None:
    """The account's credit balance (a free call), or None in replay mode."""
    if client.mode == "replay":
        return None
    return float((await client.get("/account/credits")).body["account"]["credits"])


async def check_balance(ledger: Ledger, client: CrustClient, run_id: str) -> BalanceCheck:
    run = ledger.run(run_id)
    start = run["details"].get("starting_balance") if run else None
    now = await account_balance(client)
    drop = None if start is None or now is None else round(start - now, 6)
    return BalanceCheck(ledger.credits_charged(run_id), drop)
