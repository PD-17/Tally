"""One research job, start to finish.

    read the job -> find the companies -> buy, one best purchase at a time -> finish each answer

Stopping is always safe: a cut, the deadline, a kill or the planner deciding
nothing more is worth buying all end the loop, and every case still gets its
best honest answer so far. Running the same `run_id` again resumes: past
purchases come back from the ledger for free.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import httpx

from tally.config import Settings
from tally.crust.client import CrustClient
from tally.crust.errors import EndpointForbidden, OutOfCredits, SourceTimeout
from tally.crust.fake import FakeCrustdata
from tally.intake import read_job
from tally.ledger import Ledger
from tally.llm import Brain
from tally.models import Answer, Company, JobSpec, Purchase, Source, Status
from tally.planner import Action, Case, Planner, Step
from tally.prices import PriceBook
from tally.probes import Buyer, Bought
from tally.quoter import PilotCompany, Quote, make_quote
from tally.recovery import BalanceCheck, account_balance, check_balance, reopen_after_crash, settle_in_doubt
from tally.verifier import NewsFinding, TitleVerdict, best_candidate, read_news
from tally.wallet import Approver, BudgetNode, BudgetRefused, Wallet

EventSink = Callable[[str, dict], None]

PILOT_SIZE = 5  # companies done properly before the quote
APPROVAL_SHARE = 0.25  # a single data purchase above this share of the budget asks a person...
MIN_APPROVAL_CREDITS = 20.0  # ...if it is also above this (so small budgets aren't all questions)
ANSWERED = (Status.CONFIRMED, Status.PROBABLE, Status.CONFLICT)
# "Get a quote" may spend at most this share of the budget (shown on the button before the click):
# a sample of the job, priced honestly. Everything else waits for Accept.
QUOTE_SHARE = 0.2
LISTING_SHARE = 0.5  # of what a phase may spend, the most that listing companies may take


def quote_cap(budget: float) -> float:
    """The most "Get a quote" can spend for this budget."""
    return round(budget * QUOTE_SHARE, 2)


@dataclass
class RunResult:
    run_id: str
    spec: JobSpec
    answers: list[Answer]
    stop_reason: str
    credits: float  # from the ledger
    model_cost_inr: float | None
    balance: BalanceCheck | None = None  # ledger against Crustdata's own balance
    log: list[str] = field(default_factory=list)  # the planner's reasons, in order


def new_run_id() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


class Run:
    def __init__(self, settings: Settings, job_text: str, budget: float, minutes: float,
                 run_id: str | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 approver: Approver | None = None, on_event: EventSink | None = None,
                 brain: Brain | None = None, today: date | None = None, ledger: Ledger | None = None,
                 planner: type[Planner] = Planner):
        self.settings = settings
        self.job_text = job_text
        self.run_id = run_id or new_run_id()
        self.today = today or date.today()
        self.emit = on_event or (lambda kind, data: None)
        self.ledger = ledger or Ledger(settings.data_dir / "ledger.sqlite")
        self.log: list[str] = []

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
        root = BudgetNode("client", budget)
        # Setting the budget approves data purchases inside it; one purchase bigger than a quarter of
        # the budget is unusual enough to ask a person first.
        self.wallet = Wallet(root, self.ledger, self.run_id, deadline=deadline, approver=approver,
                             approve_over=max(budget * APPROVAL_SHARE, MIN_APPROVAL_CREDITS),
                             on_change=self.emit)
        self.buyer = Buyer(self.wallet, self.client, self.prices, self.ledger)
        self.brain = brain or Brain(self.ledger, settings, self.run_id)
        self.planner = planner(self.prices, self.today)

        # Filled in by quote():
        self.spec: JobSpec | None = None
        self.cases: list[Case] = []
        self.quote_card: Quote | None = None
        self.early_stop: str | None = None  # why there is nothing to deliver
        self.job_size = 0  # how many companies the job covers (the quote may list only a sample)
        self._asked_domains: set[str] = set()  # a client's domains already looked up

    async def execute(self) -> RunResult:
        """Quote, then deliver without waiting for an Accept (the command line and tests)."""
        await self.quote()
        return await self.deliver()

    async def quote(self) -> Quote | None:
        """Phase 1: read the job, list a sample of companies, do a few properly, and forecast the rest.

        Everything it buys comes out of its own budget of `quote_cap(budget)`, which the client
        sees before asking. The rest of the job is bought only in `deliver()`, after Accept.
        Returns None when there is nothing to quote (an unsupported job, no companies);
        `deliver()` then reports why.
        """
        await self._account_for_the_past()
        self.spec = await read_job(self.job_text, self.brain)
        self.ledger.update_run(self.run_id, jobspec=self.spec.model_dump())
        self.emit("job", {"spec": self.spec.model_dump()})
        if self.spec.unsupported_reason:
            self.early_stop = f"unsupported: {self.spec.unsupported_reason}"
            return None

        root = self.wallet.root
        quote_budget = root.carve(f"quote-{len(root.children) + 1}",
                                  min(quote_cap(root.limit), max(root.available(), 0.0)))
        try:
            return await self._sample_and_forecast(quote_budget)
        finally:
            root.reclaim(quote_budget)  # whatever the quote didn't spend goes back to the job

    async def _sample_and_forecast(self, node: BudgetNode) -> Quote | None:
        try:
            self.cases = [Case(c) for c in await self._companies(self.spec, node)]
        except BudgetRefused as refused:
            self.early_stop = f"stopped before listing companies: {refused.reason}"
            return None
        if not self.cases:
            self.early_stop = "no companies matched, or the quote's budget can't list any"
            return None
        self.emit("companies", {"names": [c.company.name for c in self.cases]})

        pilot = self.cases[:PILOT_SIZE]
        started = time.monotonic()
        await self._buy_until_done(self.spec, pilot, node)
        seconds_each = (time.monotonic() - started) / len(pilot)
        model_inr_each = (self.ledger.model_cost_inr(self.run_id) or 0.0) / len(pilot)
        outcomes = [PilotCompany(c.credits, c.status(self.today) in ANSWERED, seconds_each, model_inr_each)
                    for c in pilot]
        self.quote_card = make_quote(outcomes, max(self.job_size, len(self.cases)), self.wallet.root.limit,
                                     self.ledger.credits_charged(self.run_id), self.settings)
        self.ledger.update_run(self.run_id, quote=self.quote_card.as_dict())
        self.emit("quote", self.quote_card.as_dict() | {"headline": self.quote_card.headline()})
        self._note(self.quote_card.headline())
        return self.quote_card

    async def deliver(self) -> RunResult:
        """Phase 2, after Accept: list the companies the quote's sample left out, buy the rest
        of the job, then give each case its honest answer."""
        try:
            if self.early_stop:
                return await self._finish(self.spec, [], self.early_stop)
            await self._list_the_rest(self.spec)
            stop_reason = await self._buy_until_done(self.spec, self.cases, self.wallet.root)
            await self._second_opinion_on_titles(self.spec, self.cases)
            return await self._finish(self.spec, [self._answer(c) for c in self.cases], stop_reason)
        finally:
            await self.client.aclose()

    async def top_up(self, credits: float, minutes: float = 10) -> RunResult:
        """The client adds credits after seeing the result; the run continues where it stopped.

        Raising one's own ceiling is the client's decision, so it needs no further approval.
        """
        self.wallet.cut(self.wallet.root.limit + credits, actor="client top-up")
        self.wallet.extend(minutes)
        self.client = CrustClient(self.settings, transport=self._transport)  # deliver() closed the last one
        self.buyer.client = self.client
        await self._load_key_limits()
        self._note(f"topped up by {credits:g} credits and {minutes:g} minutes")
        if not self.cases:  # the budget ran out before there was anything to deliver: start properly
            self.early_stop = None
            await self.quote()
        return await self.deliver()

    async def _account_for_the_past(self) -> None:
        """New run: note the starting balance. Resumed run: settle what a crash left in doubt."""
        await self._load_key_limits()
        if self.resuming:
            for line in await settle_in_doubt(self.ledger, self.client, self.run_id, self.started_at):
                self._note(f"reconciled after restart: {line}")
        elif "starting_balance" not in self.ledger.run(self.run_id)["details"]:
            self.ledger.update_run(self.run_id, starting_balance=await account_balance(self.client))
        # What earlier attempts already spent, less what this run's sub-budgets (a quote) hold themselves.
        root = self.wallet.root
        root.spent = self.ledger.credits_charged(self.run_id) - sum(c.total_spent() for c in root.children)

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

    # ----- finding the companies -----

    async def _companies(self, spec: JobSpec, node: BudgetNode) -> list[Company]:
        """As many of the job's companies as `node` can list with half of what it has left,
        so the other half can work on them. Sets `job_size`, the job's full count."""
        share = max(node.available(), 0.0) * LISTING_SHARE
        if spec.company_domains:
            domains = sorted({d.lower().removeprefix("www.") for d in spec.company_domains})
            self.job_size = len(domains)
            asked = [d for d in domains if d not in self._asked_domains][: int(share // self.prices.identify(1))]
            if not asked:
                return []
            self._asked_domains.update(asked)
            bought = await self.buyer.identify(node, asked)
            found = [company for _, company in bought.value if company]
            unmatched = [domain for domain, company in bought.value if company is None]
            if unmatched:
                self._note(f"no company record for {', '.join(unmatched)}: left out, not guessed")
        else:
            wanted = max(spec.company_count, 1)
            per_company = self.prices.company_search(1, self.buyer.company_search_premium(spec))
            limit = min(wanted, 1000, int(share // per_company))
            if limit == 0:
                self.job_size = wanted
                return []
            found, total = (await self.buyer.find_companies(node, spec, limit)).value
            self.job_size = min(wanted, total) if total is not None else wanted
        if len(found) < self.job_size:
            self._note(f"listed {len(found)} of {self.job_size} companies so far")
        return merge_duplicates(found)[: spec.company_count]

    async def _list_the_rest(self, spec: JobSpec) -> None:
        """After Accept: list the companies the quote's sample left out, keeping the work done.
        (A company search can't skip what it returned before, so the sample is listed again.)"""
        if len(self.cases) >= self.job_size:
            return
        try:
            more = await self._companies(spec, self.wallet.root)
        except BudgetRefused as refused:
            self._note(f"couldn't list the rest of the companies ({refused.reason}); delivering {len(self.cases)}")
            return
        known = {case.company.company_id for case in self.cases}
        fresh = [Case(company) for company in more if company.company_id not in known]
        self.cases += fresh[: max(self.job_size - len(self.cases), 0)]
        self.emit("companies", {"names": [c.company.name for c in self.cases]})

    # ----- the buying loop -----

    async def _buy_until_done(self, spec: JobSpec, cases: list[Case], node: BudgetNode) -> str:
        """Buy for `cases` out of `node` (the quote's budget, or the whole job's) until nothing is worth it."""
        while True:
            action = self.planner.next_action(cases, node.available())
            if action is None:
                return "nothing left worth buying" if node.available() > 0 else "budget used"
            self._note(action.describe())
            values_before = [c.value(self.today) for c in action.cases]
            try:
                await self._do(spec, action, node)
            except BudgetRefused as refused:
                if refused.reason in ("killed", "deadline"):
                    return refused.reason
                continue  # a cut: the planner looks again with less to spend
            except SourceTimeout as timeout:
                for case in action.cases:
                    case.done.add(action.step)
                    case.held = case.held or f"{action.step.value} timed out three times"
                self._note(f"held for a person: {timeout}")
            except EndpointForbidden as forbidden:
                self.planner.blocked.add(action.step)
                self._note(f"blocked {action.step.value}: {forbidden}")
            except OutOfCredits:
                return "Crustdata credits ran out"
            self.planner.learn(action, values_before)
            for case in action.cases:
                self.emit("case", case_summary(case, self.today))

    async def _do(self, spec: JobSpec, action: Action, node: BudgetNode) -> None:
        if action.step is Step.SEARCH:
            bought = await self.buyer.people_at(node, [c.company for c in action.cases], spec.role_titles)
            for case in action.cases:
                self._pay(case, bought, share=len(action.cases))
                self._consider(case, bought.value.get(case.company.company_id, []), bought, "person search")
        elif action.step is Step.LEADERS:
            case = action.cases[0]
            bought = await self.buyer.leaders_of(node, case.company)
            self._pay(case, bought)
            self._consider(case, bought.value, bought, "company record, people section")
        elif action.step is Step.NEWS:
            case = action.cases[0]
            bought = await self.buyer.news_about(node, case.company, case.candidate)
            self._pay(case, bought)
            case.news, item = read_news(case.candidate, case.company.name, bought.value)
            if item:
                case.sources.append(Source("web_page", item["url"], "passage", item["snippet"], None,
                                           bought.call_key))
        elif action.step is Step.REREAD:
            case = action.cases[0]
            bought = await self.buyer.confirm(node, case.candidate)
            self._pay(case, bought)
            if bought.value:
                self._consider(case, [bought.value], bought, "person record")
        for case in action.cases:
            case.done.add(action.step)

    def _consider(self, case: Case, people: list, bought: Bought, where: str) -> None:
        """Keep the better of the current candidate and the best of `people`.

        The same person's newer record replaces the old one; a different person
        replaces the current one only with a strictly stronger title.
        """
        best = best_candidate(people, case.company.headcount)
        if best is None:
            return
        person, verdict = best
        current = case.candidate
        same_person = current is not None and person.person_id == current.person_id
        stronger = current is None or (verdict is TitleVerdict.STRONG and case.verdict is not TitleVerdict.STRONG)
        if not (same_person or stronger):
            return
        if same_person:  # keep details the newer record may lack
            person.profile_url = person.profile_url or current.profile_url
            person.start_date = person.start_date or current.start_date
            person.updated_at = person.updated_at or current.updated_at
        case.candidate, case.verdict = person, verdict
        case.sources = [s for s in case.sources if s.kind != "crustdata_record"]
        case.sources.append(Source("crustdata_record", person.profile_url or bought.request_id or "",
                                   f"{where}: current title", person.title, person.updated_at, bought.call_key))

    def _pay(self, case: Case, bought: Bought, share: int = 1) -> None:
        """Put this case's share of a purchase on its receipt, at what it originally cost."""
        state = self.ledger.state(bought.call_key)
        case.purchases.append(Purchase(bought.call_key, state.endpoint, state.charged / share,
                                       state.request_id))

    async def _second_opinion_on_titles(self, spec: JobSpec, cases: list[Case]) -> None:
        """Borderline titles go to the model once, in one batch. Without it, they stay 'probable'."""
        borderline = [c for c in cases if c.verdict is TitleVerdict.BORDERLINE]
        if not borderline:
            return
        opinions = await self.brain.judge_titles(spec.role, [(c.candidate.title, c.company.headcount)
                                                             for c in borderline])
        for case in borderline:
            counts = (opinions or {}).get(case.candidate.title)
            if counts is False:
                case.notes.append(f"the only leader found is {case.candidate.name} ({case.candidate.title}), "
                                  f"which the model judged is not the {spec.role}")
                case.candidate, case.verdict = None, None
            elif counts is True:
                case.notes.append("borderline title; the model judged it counts")

    # ----- answers -----

    def _answer(self, case: Case) -> Answer:
        status = case.status(self.today)
        return Answer(company=case.company, status=status, person=case.candidate,
                      reason=self._reason(case, status), sources=list(case.sources),
                      purchases=list(case.purchases))

    def _reason(self, case: Case, status: Status) -> str:
        person = case.candidate
        if status is Status.CONFIRMED:
            fresh = case.news is not NewsFinding.CORROBORATES
            why = f"record updated {person.updated_at[:10]}" if fresh and person.updated_at else "news agrees"
            return f"{person.title}, since {person.start_date or 'unknown'} ({why})"
        if status is Status.CONFLICT:
            return (f"the record (updated {(person.updated_at or '?')[:10]}) says {person.name} is {person.title}, "
                    f"but newer news says they left: both sources shown")
        if status is Status.PROBABLE:
            if case.verdict is TitleVerdict.BORDERLINE:
                return f"borderline title '{person.title}'" + (f"; {case.notes[-1]}" if case.notes else "")
            return f"record dated {(person.updated_at or 'unknown')[:10]}, not re-confirmed"
        if status is Status.CANT_VERIFY:
            checked = ["person search"] + (["the company's leadership list"] if Step.LEADERS in case.done else [])
            extra = f"; {case.notes[-1]}" if case.notes else ""
            return f"no current engineering leader on record (checked {' and '.join(checked)}){extra}"
        if status is Status.HELD:
            return f"held for a person: {case.held}"
        return "not reached before the budget or time limit"

    async def _finish(self, spec: JobSpec, answers: list[Answer], stop_reason: str) -> RunResult:
        order = [Status.CONFIRMED, Status.PROBABLE, Status.CONFLICT, Status.CANT_VERIFY, Status.HELD,
                 Status.NOT_REACHED]
        answers.sort(key=lambda a: order.index(a.status))
        balance = await check_balance(self.ledger, self.client, self.run_id)
        self.ledger.update_run(self.run_id, status="done", stop_reason=stop_reason)
        self.emit("done", {"stop_reason": stop_reason, "ledger_credits": balance.ledger_credits,
                           "balance_drop": balance.balance_drop, "matches": balance.matches})
        return RunResult(self.run_id, spec, answers, stop_reason, self.ledger.credits_charged(self.run_id),
                         self.ledger.model_cost_inr(self.run_id), balance, list(self.log))

    def _note(self, line: str) -> None:
        self.log.append(line)
        self.emit("note", {"text": line})


def case_summary(case: Case, today: date) -> dict:
    """A case as plain JSON, for the live screens."""
    person = case.candidate
    return {"company": case.company.name, "company_id": case.company.company_id,
            "status": case.status(today).value, "person": person.name if person else None,
            "title": person.title if person else None, "credits": round(case.credits, 4)}


def merge_duplicates(companies: list[Company]) -> list[Company]:
    """One company, one record: entries with the same id merge, keeping every alias."""
    merged: dict[int, Company] = {}
    for company in companies:
        if company.company_id in merged:
            merged[company.company_id].aliases |= company.aliases
        else:
            merged[company.company_id] = company
    return list(merged.values())
