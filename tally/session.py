"""What every paid job shares: a run record, a wallet with a ceiling and a deadline, the ledger,
the Crustdata client and crash recovery.

Running the same `run_id` again resumes: the original limits come back from the ledger, calls a
crash left open are reconciled against Crustdata's usage records, and everything already bought
is served from the ledger for free.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from typing import Callable

import httpx

from tally.config import Settings
from tally.crust.client import CrustClient
from tally.crust.fake import FakeCrustdata
from tally.ledger import Ledger
from tally.llm import Brain
from tally.prices import PriceBook
from tally.recovery import account_balance, reopen_after_crash, settle_in_doubt
from tally.wallet import Approver, BudgetNode, Wallet

EventSink = Callable[[str, dict], None]

APPROVAL_SHARE = 0.25  # a single data purchase above this share of the budget asks a person...
MIN_APPROVAL_CREDITS = 20.0  # ...if it is also above this (so small budgets aren't all questions)


def new_run_id() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


class Session:
    def __init__(self, settings: Settings, job_text: str, budget: float, minutes: float,
                 run_id: str | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 approver: Approver | None = None, on_event: EventSink | None = None,
                 brain: Brain | None = None, today: date | None = None, ledger: Ledger | None = None):
        self.settings = settings
        self.job_text = job_text
        self.run_id = run_id or new_run_id()
        self.today = today or date.today()
        self.emit = on_event or (lambda kind, data: None)
        self.ledger = ledger or Ledger(settings.data_dir / "ledger.sqlite")
        self.log: list[str] = []
        self.early_stop: str | None = None  # why there is nothing to deliver

        existing = self.ledger.run(self.run_id)
        self.resuming = existing is not None
        if self.resuming:  # keep the original limits and deadline, and reopen what a crash left behind
            budget, deadline = existing["credit_limit"], existing["deadline_at"]
            self.started_at = existing["created_at"]
            for key in reopen_after_crash(self.ledger, self.run_id):
                self._note(f"found call {key[:12]} open after a restart: in doubt until reconciled")
        else:
            deadline = time.time() + minutes * 60
            self.started_at = time.time()
            self.ledger.start_run(self.run_id, job_text, budget, deadline, settings.mode)

        if transport is None and settings.mode == "fake":
            transport = FakeCrustdata().transport()
        self._transport = transport
        self.client = CrustClient(settings, transport=transport)
        self.prices = PriceBook()
        # Setting the budget approves data purchases inside it; one purchase bigger than a quarter of
        # the budget is unusual enough to ask a person first.
        self.wallet = Wallet(BudgetNode("client", budget), self.ledger, self.run_id, deadline=deadline,
                             approver=approver, approve_over=max(budget * APPROVAL_SHARE, MIN_APPROVAL_CREDITS),
                             on_change=self.emit)
        self.brain = brain or Brain(self.ledger, settings, self.run_id)

    async def execute(self):
        """Quote, then deliver without waiting for an Accept (the command line and tests)."""
        await self.quote()
        return await self.deliver()

    async def quote(self):
        raise NotImplementedError

    async def deliver(self):
        raise NotImplementedError

    async def _account_for_the_past(self) -> None:
        """New run: note the starting balance. Resumed run: settle what a crash left in doubt."""
        await self._load_key_limits()
        if self.resuming:
            for line in await settle_in_doubt(self.ledger, self.client, self.run_id, self.started_at):
                self._note(f"reconciled after restart: {line}")
        elif "starting_balance" not in self.ledger.run(self.run_id)["details"]:
            self.ledger.update_run(self.run_id, starting_balance=await account_balance(self.client))
        self.wallet.root.spent = self.ledger.credits_charged(self.run_id)  # what earlier attempts already spent

    async def _load_key_limits(self) -> None:
        """This key's own prices and rate limits, from the free endpoint list. Replay has none saved;
        if the call fails, the defaults (the docs' list prices) stay."""
        if self.client.mode == "replay":
            return
        try:
            await self.prices.load(self.client)
            await self.client.load_rate_limits()
        except Exception as error:  # a free lookup must never stop a run
            self._note(f"kept default prices and rate limits: couldn't read /account/endpoints ({error})")

    def _note(self, line: str) -> None:
        self.log.append(line)
        self.emit("note", {"text": line})
