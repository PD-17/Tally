"""One deck checked, start to finish, on the same wallet and ledger as a research job.

    read the deck -> identify the company (catches a lookalike domain) -> buy its record
    -> QUOTE (claims checked at each budget) -> accept -> comparables -> checks, best value first
    -> each claim's verdict from fixed rules -> questions, held for the partner's approval

Stopping is always safe: a cut, the deadline or a kill ends the checks, and every claim still
gets its honest verdict, "not checked at this budget" included. The same run id resumes for free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from tally.config import Settings
from tally.crust.client import CrustClient
from tally.crust.errors import BadRequest, EndpointForbidden, OutOfCredits, SourceTimeout
from tally.diligence.claims import Evidence, Judgement, Verdict, find_person, judge, person_key
from tally.diligence.deck import Deck, DeckReading, find_instructions, load_deck, merge_readings, quote_in_text, \
    read_deck_with_rules
from tally.diligence.evidence import Comparable
from tally.diligence.planner import MATERIALITY, Check, ClaimCase, DiligencePlanner
from tally.diligence.probes import DeckBuyer, domain_check
from tally.ledger import Ledger
from tally.llm import Brain
from tally.models import Purchase, Source
from tally.quoter import PRICE_MULTIPLE
from tally.recovery import BalanceCheck, check_balance
from tally.runner import EventSink, Run
from tally.wallet import Approver, BudgetNode, BudgetRefused

COMPARABLES_SHARE = 0.15  # of the budget, set aside so comparables never eat into claim checks
COMPARABLES_MAX = 6.0


@dataclass
class ClaimResult:
    index: int
    claim: object  # DeckClaim
    verdict: Verdict
    reason: str
    sources: list[Source]
    purchases: list[Purchase]
    provisional: bool
    quote_found: bool

    @property
    def credits(self) -> float:
        return sum(p.credits for p in self.purchases)


@dataclass
class MemoResult:
    run_id: str
    reading: DeckReading
    company: str
    domain_flag: str | None
    instructions: list[str]
    results: list[ClaimResult]
    comparables: list[Comparable]
    questions: list[str]
    binding: str
    stop_reason: str
    credits: float
    model_cost_inr: float | None
    balance: BalanceCheck | None = None
    quote: dict | None = None
    log: list[str] = field(default_factory=list)


class DiligenceRun(Run):
    def __init__(self, settings: Settings, deck: Deck | None, budget: float, minutes: float,
                 founders: list[str] | None = None, domain: str | None = None, run_id: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, approver: Approver | None = None,
                 on_event: EventSink | None = None, brain: Brain | None = None, today: date | None = None,
                 ledger: Ledger | None = None):
        label = f"Deck check: {deck.name}" if deck else "Deck check"
        super().__init__(settings, label, budget, minutes, run_id=run_id, transport=transport, approver=approver,
                         on_event=on_event, brain=brain, today=today, ledger=ledger, planner=DiligencePlanner)
        self.buyer = DeckBuyer(self.wallet, self.client, self.prices, self.ledger)
        self.deck = deck or self._saved_deck()
        if self.deck is None:
            raise ValueError(f"no deck given and none saved for run {self.run_id}")
        self._save_deck()
        self.founder_hint = list(founders or [])
        self.domain_hint = domain
        self.reading: DeckReading | None = None
        self.company_name = ""
        self.domain_flag: str | None = None
        self.instructions: list[str] = []
        self.evidence = Evidence(inr_per_usd=settings.inr_per_usd)
        self.claims: list[ClaimCase] = []
        self.comparables: list[Comparable] = []
        self.comps_node: BudgetNode | None = None
        self.quote_card: dict | None = None
        self.industry: str | None = None

    # ----- the deck on disk, so a restart can carry on -----

    def _deck_path(self) -> Path:
        return self.settings.data_dir / "decks" / self.run_id

    def _save_deck(self) -> None:
        path = self._deck_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self.deck.pdf if self.deck.pdf else "\n---\n".join(self.deck.pages).encode("utf-8")
            path.write_bytes(data)

    def _saved_deck(self) -> Deck | None:
        path = self._deck_path()
        return load_deck(path, name=path.name) if path.exists() else None

    # ----- phase 1: read, identify, buy the record, quote -----

    async def quote(self) -> dict | None:
        await self._account_for_the_past()
        self.reading = await self._read()
        self.instructions = list(self.reading.reader_instructions)
        self.emit("deck", {"claims": len(self.reading.claims), "instructions": self.instructions,
                           "company": self.reading.company_name})
        for text in self.instructions:
            self._note(f"instruction found in the deck, ignored: \"{text[:120]}\"")
        if not self.reading.claims:
            self.early_stop = "no checkable claims found in the deck"
            return None
        root = self.wallet.root
        try:
            found = await self.buyer.identify_company(root, self.reading.company_domain, self.reading.company_name)
            company, self.domain_flag = domain_check(self.reading.company_domain, *found.value)
            if self.domain_flag:
                self._note(f"domain check: {self.domain_flag}")
                self.emit("flag", {"text": self.domain_flag})
            if company is None:
                self.early_stop = (f"no company record for {self.reading.company_name} "
                                   f"({self.reading.company_domain or 'no domain'}): every claim is can't verify")
                return None
            self.company_name = company.name
            profile = await self.buyer.company_profile(root, company)
        except BudgetRefused as refused:
            self.early_stop = f"stopped before the company record: {refused.reason}"
            return None
        self.evidence.profile = profile.value
        if profile.value is None:
            self.early_stop = f"Crustdata has no company record for {company.name}"
            return None
        self.claims = [ClaimCase(i, c, judge(c, i, self.evidence)) for i, c in enumerate(self.reading.claims)]
        share = min(COMPARABLES_MAX, COMPARABLES_SHARE * root.limit, max(root.available(), 0))
        self.comps_node = root.carve("comparables", share) if share > 0 else None
        self.quote_card = self._make_quote()
        self.ledger.update_run(self.run_id, quote=self.quote_card)
        self.emit("quote", self.quote_card)
        self._note(self.quote_card["headline"])
        for case in self.claims:
            self.emit("claim", self._claim_event(case))
        return self.quote_card

    async def _read(self) -> DeckReading:
        saved = self.ledger.run(self.run_id)["details"].get("deck_reading")
        if saved:  # a restart: the deck was read (and paid for) already
            return DeckReading.model_validate(saved)
        rules = read_deck_with_rules(self.deck, founders=self.founder_hint, domain=self.domain_hint)
        model = await self.brain.read_deck(self.deck.pdf, self.deck.pages, self.founder_hint)
        reading = merge_readings(model, rules)
        if self.founder_hint:
            reading.founders = self.founder_hint
        if self.domain_hint:
            reading.company_domain = self.domain_hint
        reading.reader_instructions = list(dict.fromkeys(
            reading.reader_instructions + [t for _, t in find_instructions(self.deck)]))
        self.ledger.update_run(self.run_id, deck_reading=reading.model_dump(mode="json"))
        return reading

    def _make_quote(self) -> dict:
        spent = self.ledger.credits_charged(self.run_id)
        comps = self.comps_node.limit if self.comps_node else 0.0
        planned = self.planner.plan(self.claims, self.evidence)
        settled = sum(c.judgement.verdict is not Verdict.NOT_CHECKED and not c.judgement.provisional
                      for c in self.claims)
        checked, cost, curve = settled, spent + comps, [(round(spent + comps, 2), settled)]
        touched: set[int] = {c.index for c in self.claims if c.judgement.verdict is not Verdict.NOT_CHECKED
                             and not c.judgement.provisional}
        for check in planned:
            cost += check.cost
            new = [i for i in check.claims if i not in touched]
            touched.update(new)
            checked += len(new)
            curve.append((round(cost, 2), checked))
        at_budget = max((n for b, n in curve if b <= self.wallet.root.limit + 1e-9), default=settled)
        data_inr = self.settings.credits_to_inr(cost)
        model_inr = self.ledger.model_cost_inr(self.run_id) or 0.0
        cost_inr = None if data_inr is None else data_inr + model_inr
        total = len(self.claims)
        headline = (f"{total} claims in the deck. The company record already settles {settled}. "
                    f"For {self.wallet.root.limit:.0f} credits, {at_budget} of {total} get checked; a full check costs "
                    f"about {cost:.1f} credits" + (f" (₹{cost_inr * PRICE_MULTIPLE:,.0f} at our price)"
                                                     if cost_inr is not None else "") + ".")
        return {"claims": total, "settled_by_record": settled, "checked_at_budget": at_budget,
                "budget": self.wallet.root.limit, "full_check_credits": round(cost, 2), "comparables_credits": comps,
                "curve": [list(p) for p in curve], "planned": [k.describe() for k in planned],
                "cost_inr": None if cost_inr is None else round(cost_inr, 2),
                "price_inr": None if cost_inr is None else round(cost_inr * PRICE_MULTIPLE, 2),
                "headline": headline}

    # ----- phase 2: comparables, then the checks -----

    async def deliver(self) -> MemoResult:
        try:
            if self.early_stop:
                return await self._finish_memo(self.early_stop)
            if not self.comparables:
                await self._buy_comparables()
            stop = await self._check_until_done()
            return await self._finish_memo(stop)
        finally:
            await self.client.aclose()

    async def top_up(self, credits: float, minutes: float = 5) -> MemoResult:
        """More credits after the memo: the checks continue where the budget stopped them."""
        self.wallet.cut(self.wallet.root.limit + credits, actor="partner top-up")
        self.wallet.extend(minutes)
        self.client = CrustClient(self.settings, transport=self._transport)  # deliver() closed the last one
        self.buyer.client = self.client
        await self._load_key_limits()
        self._note(f"topped up by {credits:g} credits and {minutes:g} minutes")
        if not self.claims:
            self.early_stop = None
            await self.quote()
        return await self.deliver()

    async def _buy_comparables(self) -> None:
        node = self.comps_node or self.wallet.root
        try:
            bought = await self.buyer.comparables(node, self.evidence.profile, self.industry)
            self.comparables = bought.value
            self._note(f"{len(self.comparables)} comparables for {bought.credits:.2f} credits")
        except BudgetRefused as refused:
            self._note(f"comparables not bought: {refused.reason}")
        except (EndpointForbidden, BadRequest, SourceTimeout) as error:
            self._note(f"comparables not available: {error}")

    async def _check_until_done(self) -> str:
        while True:
            check = self.planner.next_action(self.claims, self.evidence, self.wallet.root.available())
            if check is None:
                return "nothing left worth checking" if self.wallet.root.available() > 0 else "budget used"
            self._note(check.describe())
            try:
                await self._do(check)
                self.planner.done.add(check.key)
            except BudgetRefused as refused:
                if refused.reason in ("killed", "deadline"):
                    return refused.reason
                if refused.reason == "denied":  # a person said no: never ask for it again
                    self.planner.blocked.add(check.key)
                # over_budget (a cut): the planner looks again with what's left
            except SourceTimeout as timeout:
                self.planner.done.add(check.key)
                for i in check.claims:
                    self.claims[i].held = f"{check.why} timed out three times"
                self._note(f"held for a person: {timeout}")
            except (EndpointForbidden, BadRequest) as error:
                self.planner.blocked.add(check.key)
                self._note(f"blocked {check.key}: {error}")
            except OutOfCredits:
                return "Crustdata credits ran out"
            self._rejudge()

    async def _do(self, check: Check) -> None:
        root, ev = self.wallet.root, self.evidence
        if check.key == "jobs":
            ev.jobs = (await self.buyer.job_posts(root, ev.profile)).value
        elif check.key.startswith("founder:"):
            name = next(self.claims[i].claim.person for i in check.claims)
            bought = await self.buyer.founder(root, name, find_person(name, ev), ev.profile)
            if bought.value:
                ev.people[person_key(name)] = bought.value
            else:  # nobody by that name at this company: remember we looked
                ev.people[person_key(name)] = find_person(name, ev) or _nobody(name, bought.call_key)
        elif check.key.startswith("web:"):
            case = self.claims[int(check.key.split(":")[1])]
            bought = await self.buyer.web(root, web_query(case.claim, ev.profile.name), check.why)
            ev.web[case.index] = bought.value

    def _rejudge(self) -> None:
        for case in self.claims:
            before = case.judgement.verdict
            case.judgement = judge(case.claim, case.index, self.evidence)
            if case.judgement.verdict is not before:
                self.emit("claim", self._claim_event(case))

    # ----- the memo -----

    def _results(self) -> list[ClaimResult]:
        users: dict[str, int] = {}
        for case in self.claims:
            for key in {s.call_key for s in case.judgement.sources if s.call_key}:
                users[key] = users.get(key, 0) + 1
        results = []
        for case in self.claims:
            j = case.judgement
            verdict, reason = j.verdict, j.reason
            if case.held and verdict is Verdict.NOT_CHECKED:
                verdict, reason = Verdict.HELD, f"held for a person: {case.held}"
            purchases = []
            for key in dict.fromkeys(s.call_key for s in j.sources if s.call_key):
                state = self.ledger.state(key)
                if state:
                    purchases.append(Purchase(key, state.endpoint, state.charged / users[key], state.request_id))
            results.append(ClaimResult(case.index, case.claim, verdict, reason, list(j.sources), purchases,
                                       j.provisional, quote_in_text(case.claim, self.deck)))
        order = [Verdict.CONTRADICTED, Verdict.RECORDS_DISAGREE, Verdict.CANT_VERIFY, Verdict.CONFIRMED,
                 Verdict.HELD, Verdict.NOT_CHECKED]
        results.sort(key=lambda r: (order.index(r.verdict), -MATERIALITY[r.claim.kind], r.index))
        return results

    async def _finish_memo(self, stop_reason: str) -> MemoResult:
        from tally.diligence.memo import build_questions, questions_binding

        reading = self.reading or DeckReading(company_name=self.deck.name, company_domain=None, founders=[],
                                              claims=[], reader_instructions=[])
        if self.early_stop and not self.claims:
            why = self.early_stop
            verdict = Verdict.NOT_CHECKED if why.startswith("stopped") else Verdict.CANT_VERIFY
            self.claims = [ClaimCase(i, c, Judgement(verdict, why)) for i, c in enumerate(reading.claims)]
        results = self._results()
        questions = build_questions(results, self.domain_flag, self.company_name or reading.company_name)
        binding = questions_binding(self.run_id, questions)
        balance = await check_balance(self.ledger, self.client, self.run_id)
        self.ledger.update_run(self.run_id, status="done", stop_reason=stop_reason, questions=questions,
                               questions_binding=binding)
        self.emit("done", {"stop_reason": stop_reason, "ledger_credits": balance.ledger_credits,
                           "balance_drop": balance.balance_drop, "matches": balance.matches})
        return MemoResult(self.run_id, reading, self.company_name or reading.company_name, self.domain_flag,
                          self.instructions, results, self.comparables, questions, binding, stop_reason,
                          self.ledger.credits_charged(self.run_id), self.ledger.model_cost_inr(self.run_id),
                          balance, self.quote_card, list(self.log))

    def _claim_event(self, case: ClaimCase) -> dict:
        return {"index": case.index, "kind": case.claim.kind.value, "text": case.claim.text,
                "verdict": case.judgement.verdict.value, "reason": case.judgement.reason,
                "provisional": case.judgement.provisional}


def web_query(claim, company: str) -> str:
    from tally.diligence.deck import ClaimKind

    subject = claim.subject or ""
    if claim.kind is ClaimKind.INVESTOR:
        return f'"{subject}" invests in {company}'
    if claim.kind in (ClaimKind.FUNDING_TOTAL, ClaimKind.LAST_ROUND):
        return f"{company} raises {subject} funding round".replace("  ", " ")
    if subject:
        return f'{company} "{subject}"'
    return f"{company} {claim.text[:80]}"


def _nobody(name: str, call_key: str):
    from tally.diligence.evidence import PersonRecord

    return PersonRecord(name=name, call_key=call_key)


def now() -> float:
    return time.time()
