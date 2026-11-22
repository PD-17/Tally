"""The web app: the desk (laptop) and the dial (the judge's phone), over one live event stream.

    uv run tally serve              then open http://localhost:8000

Flow: POST a job -> the quote appears -> Accept -> the run (and, if asked, the
race against the naive buyer) streams live -> receipts and the audit screen.
"""

from __future__ import annotations

import asyncio
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
from tally.events import EventHub
from tally.ledger import Ledger
from tally.planner import EnrichEverything
from tally.race import scoreboard
from tally.recovery import account_balance
from tally.report import bill, receipt
from tally.runner import QUOTE_SHARE, Run, RunResult, new_run_id
from tally.vendor import audit_offer, make_offer
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


class JobRequest(BaseModel):
    job: str
    budget: float
    minutes: float = 20
    race: bool = True


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
        self.runs: dict[str, Run] = {}
        self.approvers: dict[str, WebApprover] = {}
        self.results: dict[str, RunResult] = {}
        self.audits: dict[str, dict] = {}
        self.wants_race: dict[str, bool] = {}

    def make_run(self, job_text: str, budget: float, minutes: float, run_id: str | None = None,
                 planner=None) -> Run:
        run_id = run_id or new_run_id()
        approver = WebApprover(self.hub, run_id)
        kwargs = {"planner": planner} if planner else {}
        run = Run(self.settings, job_text, budget, minutes, run_id=run_id, ledger=self.ledger,
                  transport=self.world.transport() if self.world else None, approver=approver,
                  on_event=self.hub.sink(run_id), **kwargs)
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

    def run(self, run_id: str) -> Run:
        if run_id not in self.runs:
            raise HTTPException(404, f"no run {run_id} in this server")
        return self.runs[run_id]

    async def deliver(self, run_id: str) -> None:
        run = self.run(run_id)
        jobs = [run.deliver()]
        if self.wants_race.get(run_id):
            naive = self.make_run(run.job_text, run.wallet.root.limit, run.wallet.seconds_left() / 60,
                                  run_id=f"{run_id}-baseline", planner=EnrichEverything)
            jobs.append(naive.execute())
        results = await asyncio.gather(*jobs)
        self.audits[run_id] = await self.balance_audit(run_id, [r.run_id for r in results])
        for result in results:
            self.results[result.run_id] = result
        if len(results) == 2:
            self.hub.publish(run_id, "scoreboard", scoreboard(*results))

    async def top_up(self, run_id: str, credits: float) -> None:
        """More credits after the result: the run continues, then report and audit are redone."""
        run = self.run(run_id)
        self.results.pop(run_id, None)  # the screens wait for the new result
        self.audits.pop(run_id, None)
        result = await run.top_up(credits)
        lanes = [run_id] + ([f"{run_id}-baseline"] if f"{run_id}-baseline" in self.runs else [])
        self.audits[run_id] = await self.balance_audit(run_id, lanes)
        self.results[run_id] = result

    async def balance_audit(self, run_id: str, run_ids: list[str]) -> dict:
        """Ledger against Crustdata's balance since `run_id` started. Raced runs share one
        account, so both lanes' ledgers are added together."""
        start = self.ledger.run(run_id)["details"].get("starting_balance")
        client = CrustClient(self.settings, transport=self.world.transport() if self.world else None)
        try:
            now = await account_balance(client)
        finally:
            await client.aclose()
        ledger_credits = round(sum(self.ledger.credits_charged(r) for r in run_ids), 6)
        drop = None if start is None or now is None else round(start - now, 6)
        return {"runs": run_ids, "ledger_credits": ledger_credits, "balance_drop": drop,
                "matches": None if drop is None else abs(ledger_credits - drop) < 0.005}


def create_app(settings: Settings | None = None) -> FastAPI:
    desk = Desk(settings or load_settings())
    app = FastAPI(title="Tally")
    app.state.desk = desk

    @app.get("/")
    async def desk_page():
        return FileResponse(STATIC / "desk.html")

    @app.get("/phone")
    async def phone_page():
        return FileResponse(STATIC / "phone.html")

    @app.get("/api/info")
    async def info():
        """Mode (for the REPLAY / FAKE banner), the address phones on the same Wi-Fi can use, and the
        share of the budget a quote may spend (the desk shows it on the button before the click)."""
        return {"mode": desk.settings.mode, "lan_url": f"http://{_lan_ip()}:8000", "quote_share": QUOTE_SHARE}

    @app.post("/api/jobs")
    async def start_job(request: JobRequest):
        run = desk.make_run(request.job, request.budget, request.minutes)
        desk.wants_race[run.run_id] = request.race
        desk.in_background(run.run_id, run.quote())
        return {"run_id": run.run_id}

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
        run = desk.make_run(record["job_text"], record["credit_limit"], 0, run_id=run_id)

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

    @app.post("/api/jobs/{run_id}/vendor")
    async def vendor(run_id: str):
        """A simulated vendor pitches a list; Tally audits a sample against its own receipts."""
        if run_id not in desk.results:
            raise HTTPException(409, "the vendor pitches after the run has finished")
        answers = desk.results[run_id].answers
        audit = audit_offer(make_offer(answers), answers).as_dict()
        desk.ledger.update_run(run_id, vendor_audit=audit)
        return audit

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

    @app.get("/api/jobs/{run_id}/report")
    async def report(run_id: str):
        result = desk.results.get(run_id)
        if result is None:
            raise HTTPException(409, "the run hasn't finished")
        return {"bill": bill(result, desk.ledger, desk.settings),
                "rows": [receipt(a, desk.ledger) for a in result.answers]}

    @app.get("/api/jobs/{run_id}/audit")
    async def audit(run_id: str):
        run = desk.run(run_id)
        if run_id not in desk.audits:
            raise HTTPException(409, "the run hasn't finished")
        chain = desk.ledger.verify_chain()
        return {**desk.audits[run_id], "chain_ok": chain.ok, "entries": chain.entries,
                "first_bad_entry": chain.first_bad_seq, "tally_credits": desk.ledger.credits_charged(run_id),
                "quote": run.quote_card.as_dict() if run.quote_card else None,
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

