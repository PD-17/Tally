"""The ledger: an append-only, hash-chained record of every call Tally pays for.

Three rules make it trustworthy:

1. Write before you buy. A paid call is written as `reserved` and committed
   before the HTTP request goes out, then `settled` with the real cost after.
2. Never edit, only append. A call's state is its latest entry. Each entry
   stores `row_hash = sha256(prev_hash + entry)`, so `verify_chain()` can
   point to the first entry anyone changed.
3. When unsure, count it. A call that was reserved but never settled (the
   process died mid-call) becomes `in_doubt` and counts at its reserved
   amount until reconciliation settles it. The ledger may overcount for a
   moment but never misses a charge.

Model calls and payments go in the same chain, so every cost of a job is here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

Kind = Literal["crustdata", "model", "payment"]
Event = Literal["reserved", "settled", "failed", "in_doubt", "resolved", "cached"]

GENESIS_HASH = "0" * 64

# The columns that are hashed, in order. Changing any of them breaks the chain.
HASHED_COLUMNS = (
    "run_id", "call_key", "kind", "event", "budget_id", "endpoint", "reason",
    "credits", "model", "tokens_in", "tokens_out", "cost_inr", "request_id", "note", "at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id           TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    job_text     TEXT NOT NULL,
    credit_limit REAL NOT NULL,
    deadline_at  REAL NOT NULL,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    details      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS entries(
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    call_key   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    event      TEXT NOT NULL,
    budget_id  TEXT,
    endpoint   TEXT,
    reason     TEXT,
    credits    REAL,
    model      TEXT,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    cost_inr   REAL,
    request_id TEXT,
    note       TEXT,
    at         REAL NOT NULL,
    prev_hash  TEXT NOT NULL,
    row_hash   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_by_call ON entries(call_key, seq);
CREATE INDEX IF NOT EXISTS entries_by_run ON entries(run_id, seq);
CREATE TABLE IF NOT EXISTS responses(
    call_key TEXT PRIMARY KEY,
    body     TEXT NOT NULL
);
"""


def canonical_json(value: Any) -> str:
    """One fixed JSON spelling for a value, so equal requests hash equally."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def call_key(run_id: str, endpoint: str, body: Any, attempt: int = 0) -> str:
    """The idempotency key of a purchase: the same request in the same run is bought once.

    `attempt` separates deliberate re-buys (after a timeout) from repeats.
    """
    return sha256(f"{run_id}|{endpoint}|{canonical_json(body)}|{attempt}")


@dataclass(frozen=True)
class CallState:
    """Where one call stands, taken from its latest ledger entry."""

    call_key: str
    run_id: str
    kind: Kind
    event: Event
    endpoint: str | None
    budget_id: str | None
    reason: str | None
    reserved: float  # credits reserved before the call
    charged: float  # credits counted as spent (see `ledger rule 3`)
    request_id: str | None
    cost_inr: float | None


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    entries: int
    first_bad_seq: int | None = None


class Ledger:
    """SQLite-backed ledger. One instance per process; safe to share across asyncio tasks."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")  # a committed entry survives a hard kill
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ----- runs -----------------------------------------------------------

    def start_run(self, run_id: str, job_text: str, credit_limit: float, deadline_at: float,
                  mode: str, client_id: str = "default") -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO runs(id, client_id, job_text, credit_limit, deadline_at, mode, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                (run_id, client_id, job_text, credit_limit, deadline_at, mode, time.time()),
            )

    def run(self, run_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        run["details"] = json.loads(run["details"])
        return run

    def set_limits(self, run_id: str, credit_limit: float, deadline_at: float) -> None:
        """Persist a cut, top-up or extension, so a restarted run keeps the client's latest limits."""
        with self._lock:
            self._db.execute("UPDATE runs SET credit_limit = ?, deadline_at = ? WHERE id = ?",
                             (credit_limit, deadline_at, run_id))

    def update_run(self, run_id: str, status: str | None = None, **details: Any) -> None:
        """Set the run's status and merge extra details (quote, jobspec, ...)."""
        with self._lock:
            current = self.run(run_id)
            if current is None:
                raise KeyError(run_id)
            merged = {**current["details"], **details}
            self._db.execute(
                "UPDATE runs SET status = COALESCE(?, status), details = ? WHERE id = ?",
                (status, canonical_json(merged), run_id),
            )

    # ----- the purchase protocol -----------------------------------------

    def reserve(self, run_id: str, key: str, budget_id: str, endpoint: str,
                credits: float, reason: str) -> None:
        """Step 1: record the intent to buy, before the request goes out."""
        self._append(run_id=run_id, call_key=key, kind="crustdata", event="reserved",
                     budget_id=budget_id, endpoint=endpoint, reason=reason, credits=credits)

    def settle(self, key: str, credits: float, request_id: str | None, body: Any) -> None:
        """Step 2: the response arrived; store it and the real cost together."""
        state = self._require(key)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("INSERT OR REPLACE INTO responses(call_key, body) VALUES (?, ?)",
                                 (key, canonical_json(body)))
                self._append_locked(run_id=state.run_id, call_key=key, kind="crustdata", event="settled",
                                    budget_id=state.budget_id, endpoint=state.endpoint, credits=credits,
                                    request_id=request_id)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def fail(self, key: str, note: str, credits: float = 0.0, request_id: str | None = None) -> None:
        """The call returned an error. Errors are normally free (X-Credits-Used: 0)."""
        state = self._require(key)
        self._append(run_id=state.run_id, call_key=key, kind="crustdata", event="failed",
                     budget_id=state.budget_id, endpoint=state.endpoint, credits=credits,
                     request_id=request_id, note=note)

    def mark_in_doubt(self, key: str, note: str) -> None:
        """We can't tell whether Crustdata charged us (a timeout, or a crash mid-call)."""
        state = self._require(key)
        self._append(run_id=state.run_id, call_key=key, kind="crustdata", event="in_doubt",
                     budget_id=state.budget_id, endpoint=state.endpoint, credits=state.reserved, note=note)

    def resolve(self, key: str, credits: float, note: str, request_id: str | None = None) -> None:
        """Reconciliation found out what an in-doubt call really cost."""
        state = self._require(key)
        self._append(run_id=state.run_id, call_key=key, kind="crustdata", event="resolved",
                     budget_id=state.budget_id, endpoint=state.endpoint, credits=credits,
                     request_id=request_id, note=note)

    def record_cache_hit(self, run_id: str, key: str, endpoint: str, reason: str) -> None:
        """A repeat of a purchase already made: served from the ledger, 0 credits."""
        self._append(run_id=run_id, call_key=f"{key}:cache:{time.time_ns()}", kind="crustdata",
                     event="cached", endpoint=endpoint, reason=reason, credits=0.0)

    # ----- model calls and payments ----------------------------------------

    def record_model_call(self, run_id: str, purpose: str, model: str, tokens_in: int,
                          tokens_out: int, cost_inr: float | None) -> None:
        self._append(run_id=run_id, call_key=f"model:{time.time_ns()}", kind="model", event="settled",
                     reason=purpose, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
                     credits=0.0, cost_inr=cost_inr)

    def record_payment(self, run_id: str, key: str, amount_inr: float, note: str,
                       request_id: str | None = None) -> None:
        """A payment made in a payment provider's test mode (a vendor buy, a top-up)."""
        self._append(run_id=run_id, call_key=key, kind="payment", event="settled", credits=0.0,
                     cost_inr=amount_inr, request_id=request_id, note=note)

    # ----- reading ----------------------------------------------------------

    def saved_response(self, key: str) -> Any | None:
        """The stored response of a settled purchase, so it is never bought twice."""
        state = self.state(key)
        if state is None or state.event not in ("settled", "resolved"):
            return None
        row = self._db.execute("SELECT body FROM responses WHERE call_key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row["body"])

    def state(self, key: str) -> CallState | None:
        rows = self._db.execute("SELECT * FROM entries WHERE call_key = ? ORDER BY seq", (key,)).fetchall()
        return _fold(rows) if rows else None

    def states(self, run_id: str | None = None) -> list[CallState]:
        """The current state of every call, oldest first."""
        sql = "SELECT * FROM entries" + (" WHERE run_id = ?" if run_id else "") + " ORDER BY seq"
        rows = self._db.execute(sql, (run_id,) if run_id else ()).fetchall()
        by_key: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_key.setdefault(row["call_key"], []).append(row)
        return [_fold(group) for group in by_key.values()]

    def open_reservations(self) -> list[CallState]:
        """Calls reserved but never finished: what a crash leaves behind."""
        return [s for s in self.states() if s.event == "reserved"]

    def credits_charged(self, run_id: str | None = None) -> float:
        return round(sum(s.charged for s in self.states(run_id) if s.kind == "crustdata"), 6)

    def model_cost_inr(self, run_id: str | None = None) -> float | None:
        costs = [s.cost_inr for s in self.states(run_id) if s.kind == "model"]
        if any(c is None for c in costs):
            return None
        return round(sum(costs), 4)

    def entries(self, run_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM entries" + (" WHERE run_id = ?" if run_id else "") + " ORDER BY seq"
        return [dict(r) for r in self._db.execute(sql, (run_id,) if run_id else ()).fetchall()]

    def verify_chain(self) -> ChainCheck:
        """Recompute every hash. Any edited, deleted or inserted entry shows up here."""
        prev = GENESIS_HASH
        rows = self._db.execute("SELECT * FROM entries ORDER BY seq").fetchall()
        for row in rows:
            expected = _row_hash(prev, {c: row[c] for c in HASHED_COLUMNS})
            if row["prev_hash"] != prev or row["row_hash"] != expected:
                return ChainCheck(ok=False, entries=len(rows), first_bad_seq=row["seq"])
            prev = row["row_hash"]
        return ChainCheck(ok=True, entries=len(rows))

    # ----- internals ----------------------------------------------------------

    def _require(self, key: str) -> CallState:
        state = self.state(key)
        if state is None:
            raise KeyError(f"no reservation for call {key[:12]}")
        return state

    def _append(self, **fields: Any) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._append_locked(**fields)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _append_locked(self, **fields: Any) -> None:
        """Append one entry. The caller holds the lock and an open transaction."""
        entry = {column: fields.get(column) for column in HASHED_COLUMNS}
        entry["at"] = time.time()
        last = self._db.execute("SELECT row_hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["row_hash"] if last else GENESIS_HASH
        columns = ", ".join(HASHED_COLUMNS) + ", prev_hash, row_hash"
        marks = ", ".join("?" for _ in range(len(HASHED_COLUMNS) + 2))
        self._db.execute(f"INSERT INTO entries({columns}) VALUES ({marks})",
                         (*entry.values(), prev, _row_hash(prev, entry)))


def _row_hash(prev_hash: str, entry: dict) -> str:
    return sha256(prev_hash + canonical_json([entry[c] for c in HASHED_COLUMNS]))


def _fold(rows: Iterable[sqlite3.Row]) -> CallState:
    """Turn a call's entries, oldest first, into its current state."""
    rows = list(rows)
    first, last = rows[0], rows[-1]
    reserved = first["credits"] if first["event"] == "reserved" else 0.0
    charged = {
        "settled": last["credits"] or 0.0,
        "resolved": last["credits"] or 0.0,
        "failed": last["credits"] or 0.0,
        "in_doubt": reserved,  # rule 3: count it until we know
        "reserved": 0.0,  # still in flight; the wallet holds it as reserved
        "cached": 0.0,
    }[last["event"]]
    request_id = next((r["request_id"] for r in reversed(rows) if r["request_id"]), None)
    return CallState(
        call_key=last["call_key"], run_id=last["run_id"], kind=last["kind"], event=last["event"],
        endpoint=first["endpoint"], budget_id=first["budget_id"], reason=first["reason"],
        reserved=reserved or 0.0, charged=charged, request_id=request_id, cost_inr=last["cost_inr"],
    )
