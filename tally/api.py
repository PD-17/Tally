"""The web app: the desk (laptop) and the dial (the judge's phone), over one live event stream.

    uv run tally serve              then open http://localhost:8000

Flow: POST a deck -> the quote appears -> Accept -> the checks stream live -> the memo with a
receipt per claim -> the partner approves the founders' questions -> the audit screen.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import socket
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from tally.config import Settings, load_settings
from tally.crust.client import CrustClient
from tally.crust.fake import FakeCrustdata
from tally.diligence.deck import load_deck
from tally.diligence.memo import approve_questions
from tally.diligence.memo import as_dict as memo_dict
from tally.diligence.run import DiligenceRun, MemoResult
from tally.events import EventHub
from tally.ledger import Ledger
from tally.recovery import account_balance
from tally.session import new_run_id
from tally.wallet import ApprovalRequest

STATIC = Path(__file__).parent / "static"
log = logging.getLogger(__name__)


class WebApprover:
    """Approvals wait here until someone presses Approve or Deny on a screen."""

    def __init__(self, hub: EventHub, run_id: str):
        self.hub, self.run_id = hub, run_id
        self.pending: dict[str, asyncio.Future] = {}

    async def decide(self, request: ApprovalRequest) -> bool:
        future = asyncio.get_running_loop().create_future()
        self.pending[request.binding] = future
        self.hub.publish(self.run_id, "approval", {"binding": request.binding, "endpoint": request.endpoint,
                                                   "credits": request.credits, "reason": request.reason})
        try:
            return await future
        finally:
            self.pending.pop(request.binding, None)

    def answer(self, binding: str, approve: bool) -> bool:
        future = self.pending.get(binding)
        if future is None or future.done():
            return False
        future.set_result(approve)
        return True


class MemoRequest(BaseModel):
    deck_b64: str
    filename: str = "deck.pdf"
    founders: list[str] = []
    domain: str | None = None
    budget: float = 150
    minutes: float = 8


class QuestionsAnswer(BaseModel):
    binding: str
    approve: bool


class CutRequest(BaseModel):
    limit: float | None = None
    factor: float | None = None  # e.g. 0.5 halves what's left of the ceiling


class TopUpRequest(BaseModel):
    credits: float


class ApprovalAnswer(BaseModel):
    approve: bool


class Desk:
    """Everything one server process knows about its runs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.hub = EventHub()
        self.ledger = Ledger(settings.data_dir / "ledger.sqlite")
        self.world = FakeCrustdata() if settings.mode == "fake" else None  # one shared offline world
        self.runs: dict[str, DiligenceRun] = {}
        self.approvers: dict[str, WebApprover] = {}
        self.results: dict[str, MemoResult] = {}
        self.audits: dict[str, dict] = {}

    def make_memo(self, request: "MemoRequest") -> DiligenceRun:
        run_id = new_run_id()
        approver = WebApprover(self.hub, run_id)
        deck = load_deck(base64.b64decode(request.deck_b64), name=request.filename)
        run = DiligenceRun(self.settings, deck, request.budget, request.minutes, founders=request.founders,
                           domain=request.domain or None, run_id=run_id, ledger=self.ledger,
                           transport=self.world.transport() if self.world else None, approver=approver,
                           on_event=self.hub.sink(run_id))
        self.runs[run_id], self.approvers[run_id] = run, approver
        return run

    def in_background(self, run_id: str, work) -> None:
        """Start `work` without waiting; if it fails, the screens hear about it."""
        task = asyncio.create_task(work)

        def report_failure(done: asyncio.Task) -> None:
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                log.error("run %s failed: %r", run_id, error)
                self.hub.publish(run_id, "error", {"error": f"{type(error).__name__}: {error}"})

        task.add_done_callback(report_failure)

    def run(self, run_id: str) -> DiligenceRun:
        if run_id not in self.runs:
            raise HTTPException(404, f"no run {run_id} in this server")
        return self.runs[run_id]

    async def deliver(self, run_id: str) -> None:
        result = await self.run(run_id).deliver()
        self.audits[run_id] = await self.balance_audit(run_id)
        self.results[run_id] = result

    async def top_up(self, run_id: str, credits: float) -> None:
        """More credits after the result: the run continues, then report and audit are redone."""
        run = self.run(run_id)
        self.results.pop(run_id, None)  # the screens wait for the new result
        self.audits.pop(run_id, None)
        result = await run.top_up(credits)
        self.audits[run_id] = await self.balance_audit(run_id)
        self.results[run_id] = result

    async def balance_audit(self, run_id: str) -> dict:
        """The run's ledger against the drop in Crustdata's balance since it started."""
        start = self.ledger.run(run_id)["details"].get("starting_balance")
        client = CrustClient(self.settings, transport=self.world.transport() if self.world else None)
        try:
            now = await account_balance(client)
        finally:
            await client.aclose()
        ledger_credits = round(self.ledger.credits_charged(run_id), 6)
        drop = None if start is None or now is None else round(start - now, 6)
        return {"run": run_id, "ledger_credits": ledger_credits, "balance_drop": drop,
                "matches": None if drop is None else abs(ledger_credits - drop) < 0.005}


def create_app(settings: Settings | None = None) -> FastAPI:
    desk = Desk(settings or load_settings())
    app = FastAPI(title="Tally")
    app.state.desk = desk

    @app.get("/")
    async def desk_page():
        return FileResponse(STATIC / "memo.html")

    @app.get("/simple")
    async def simple_page():
        """A plainer screen: upload, quote, check, memo. The same API and the same approvals."""
        return FileResponse(STATIC / "simple.html")

    @app.post("/api/memos")
    async def start_memo(request: MemoRequest):
        run = desk.make_memo(request)
        desk.in_background(run.run_id, run.quote())
        return {"run_id": run.run_id}

    @app.get("/api/memos/{run_id}")
    async def memo(run_id: str):
        result = desk.results.get(run_id)
        if result is None or not isinstance(result, MemoResult):
            raise HTTPException(409, "the memo isn't finished")
        return memo_dict(result, desk.ledger, desk.settings) | {"audit": desk.audits.get(run_id)}

    @app.post("/api/memos/{run_id}/questions")
    async def questions(run_id: str, answer: QuestionsAnswer):
        try:
            outbox = approve_questions(desk.ledger, desk.settings, run_id, answer.binding, answer.approve)
        except ValueError as error:
            raise HTTPException(409, str(error))
        desk.hub.publish(run_id, "questions", {"approved": answer.approve, "outbox": str(outbox) if outbox else None})
        return {"ok": True, "outbox": str(outbox) if outbox else None}

    @app.get("/phone")
    async def phone_page():
        return FileResponse(STATIC / "phone.html")

    @app.get("/api/info")
    async def info():
        """Mode (for the REPLAY / FAKE banner) and the address phones on the same Wi-Fi can use."""
        return {"mode": desk.settings.mode, "lan_url": f"http://{_lan_ip()}:8000"}

    @app.post("/api/jobs/{run_id}/accept")
    async def accept(run_id: str):
        desk.run(run_id)
        desk.in_background(run_id, desk.deliver(run_id))
        return {"ok": True}

    @app.post("/api/jobs/{run_id}/resume")
    async def resume(run_id: str):
        """After the server was killed: the same run id continues where it stopped."""
        record = desk.ledger.run(run_id)
        if record is None:
            raise HTTPException(404, f"no run {run_id} in the ledger")
        run = DiligenceRun(desk.settings, None, record["credit_limit"], 0, run_id=run_id, ledger=desk.ledger,
                           transport=desk.world.transport() if desk.world else None,
                           approver=WebApprover(desk.hub, run_id), on_event=desk.hub.sink(run_id))
        desk.runs[run_id], desk.approvers[run_id] = run, run.wallet.approver

        async def quote_then_deliver():
            await run.quote()
            await desk.deliver(run_id)

        desk.in_background(run_id, quote_then_deliver())
        return {"ok": True}

    @app.post("/api/jobs/{run_id}/cut")
    async def cut(run_id: str, request: CutRequest):
        wallet = desk.run(run_id).wallet
        if request.limit is not None:
            new_limit = request.limit
        elif request.factor is not None:
            new_limit = wallet.spent() + (wallet.root.limit - wallet.spent()) * request.factor
        else:
            raise HTTPException(400, "send limit or factor")
        wallet.cut(new_limit, actor="phone")
        return {"limit": wallet.root.limit, "spent": wallet.spent()}

    @app.post("/api/jobs/{run_id}/topup")
    async def top_up(run_id: str, request: TopUpRequest):
        if run_id not in desk.results:
            raise HTTPException(409, "top up after the run has finished")
        desk.in_background(run_id, desk.top_up(run_id, request.credits))
        return {"ok": True}

    @app.post("/api/jobs/{run_id}/kill")
    async def kill(run_id: str):
        desk.run(run_id).wallet.kill(actor="phone")
        return {"ok": True}

    @app.post("/api/jobs/{run_id}/approvals/{binding}")
    async def approve(run_id: str, binding: str, answer: ApprovalAnswer):
        if not desk.approvers[run_id].answer(binding, answer.approve):
            raise HTTPException(404, "no such approval waiting")
        return {"ok": True}

    @app.get("/api/jobs/{run_id}/state")
    async def state(run_id: str):
        wallet = desk.run(run_id).wallet
        return {"limit": wallet.root.limit, "spent": wallet.spent(), "seconds_left": wallet.seconds_left(),
                "killed": wallet.killed, "tree": wallet.root.as_dict()}

    @app.get("/api/jobs/{run_id}/audit")
    async def audit(run_id: str):
        run = desk.run(run_id)
        if run_id not in desk.audits:
            raise HTTPException(409, "the run hasn't finished")
        chain = desk.ledger.verify_chain()
        return {**desk.audits[run_id], "chain_ok": chain.ok, "entries": chain.entries,
                "first_bad_entry": chain.first_bad_seq, "tally_credits": desk.ledger.credits_charged(run_id),
                "quote": run.quote_card,
                "entries_for_run": desk.ledger.entries(run_id)[-200:]}

    @app.get("/api/events")
    async def events(run_id: str | None = None):
        return EventSourceResponse(desk.hub.stream(run_id))

    return app


def _lan_ip() -> str:
    """This laptop's address on the local network (no packets are sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()

