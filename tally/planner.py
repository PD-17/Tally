"""Choosing the next purchase: the one with the most expected answer value per credit.

Each company is a case that moves through up to four steps:

    search people    one batched person search covers up to 20 companies (cheap)
    ask the company  Company Enrich's leadership list, when the search found nobody (2 credits)
    check the news   a web search, when the record is old: did they leave? (1 credit)
    re-read person   the person's own record, when the news said nothing (1 credit)

A step's value is how much it is expected to improve the case's answer
(confirmed > conflict > probable > can't verify). Its chance of helping
starts from a prior and is learned as the run goes, so the planner stops
buying steps that aren't paying off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from tally.models import Candidate, Company, Purchase, Source, Status
from tally.prices import PriceBook
from tally.verifier import NewsFinding, TitleVerdict, is_stale

ANSWER_VALUE = {
    Status.NOT_REACHED: 0.0,
    Status.HELD: 0.1,
    Status.CANT_VERIFY: 0.3,
    Status.PROBABLE: 0.6,
    Status.CONFLICT: 0.8,  # an honest disagreement beats a wrong fact
    Status.CONFIRMED: 1.0,
}

SEARCH_BATCH = 20
RESULTS_PER_COMPANY = 4


class Step(str, Enum):
    SEARCH = "search people"
    LEADERS = "ask the company"
    NEWS = "check the news"
    REREAD = "re-read person"


@dataclass
class Case:
    """One company and everything learned about it so far."""

    company: Company
    done: set[Step] = field(default_factory=set)
    candidate: Candidate | None = None
    verdict: TitleVerdict | None = None
    news: NewsFinding | None = None
    held: str | None = None  # why a source is held for a person
    notes: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    purchases: list[Purchase] = field(default_factory=list)

    def status(self, today: date) -> Status:
        if self.candidate is None:
            if self.held:
                return Status.HELD
            return Status.CANT_VERIFY if Step.SEARCH in self.done else Status.NOT_REACHED
        if self.news is NewsFinding.CONTRADICTS:
            return Status.CONFLICT
        fresh = not is_stale(self.candidate, today)
        if self.verdict is TitleVerdict.STRONG and (fresh or self.news is NewsFinding.CORROBORATES):
            return Status.CONFIRMED
        return Status.PROBABLE

    def value(self, today: date) -> float:
        return ANSWER_VALUE[self.status(today)]

    @property
    def credits(self) -> float:
        return sum(p.credits for p in self.purchases)


@dataclass
class SuccessRate:
    """How often a step improved an answer: a Beta(helped + a, didn't + b) mean."""

    helped: float
    did_not: float

    @property
    def chance(self) -> float:
        return self.helped / (self.helped + self.did_not)

    def learn(self, improved: bool) -> None:
        if improved:
            self.helped += 1
        else:
            self.did_not += 1


@dataclass(frozen=True)
class Action:
    step: Step
    cases: list[Case]
    cost: float  # worst-case credits
    gain: float  # expected improvement in answer value

    @property
    def value_per_credit(self) -> float:
        return self.gain / self.cost if self.cost else float("inf")

    def describe(self) -> str:
        names = ", ".join(c.company.name for c in self.cases[:3]) + (" ..." if len(self.cases) > 3 else "")
        return f"{self.step.value} ({names}): ~{self.gain:.2f} answer value for <= {self.cost:.2f} credits"


class Planner:
    def __init__(self, prices: PriceBook, today: date, floor: float = 0.02):
        self.prices = prices
        self.today = today
        self.floor = floor  # stop when nothing earns this much answer value per credit
        self.blocked: set[Step] = set()  # steps whose endpoint returned 403
        self.rates = {  # priors: what we expect before seeing any results
            Step.SEARCH: SuccessRate(4, 1),
            Step.LEADERS: SuccessRate(2, 2),
            Step.NEWS: SuccessRate(2, 2),
            Step.REREAD: SuccessRate(1, 3),
        }

    def next_action(self, cases: list[Case], available: float) -> Action | None:
        """The affordable action with the best value per credit, or None to stop."""
        options = [a for a in self._options(cases) if a.cost <= available and a.step not in self.blocked]
        worthwhile = [a for a in options if a.value_per_credit >= self.floor]
        return max(worthwhile, key=lambda a: a.value_per_credit, default=None)

    def learn(self, action: Action, values_before: list[float]) -> None:
        for case, before in zip(action.cases, values_before):
            self.rates[action.step].learn(case.value(self.today) > before)

    def _options(self, cases: list[Case]) -> list[Action]:
        options = []
        unsearched = [c for c in cases if Step.SEARCH not in c.done and not c.held]
        if unsearched:
            batch = unsearched[:SEARCH_BATCH]
            gain = sum(self.rates[Step.SEARCH].chance * (0.8 - c.value(self.today)) for c in batch)
            options.append(Action(Step.SEARCH, batch, self.prices.person_search(len(batch) * RESULTS_PER_COMPANY), gain))
        for case in cases:
            options.extend(self._single_company_options(case))
        return options

    def _single_company_options(self, case: Case) -> list[Action]:
        """Steps for one company, each priced and valued."""
        now = case.value(self.today)
        searched = Step.SEARCH in case.done
        options = []
        weak = case.candidate is None or case.verdict is TitleVerdict.BORDERLINE
        if searched and weak and Step.LEADERS not in case.done:
            gain = self.rates[Step.LEADERS].chance * (0.8 - now)
            options.append(Action(Step.LEADERS, [case], self.prices.company_enrich(1), gain))
        if case.candidate and is_stale(case.candidate, self.today) and Step.NEWS not in case.done:
            gain = self.rates[Step.NEWS].chance * (1.0 - now)
            options.append(Action(Step.NEWS, [case], self.prices.web_search(), gain))
        stale_and_quiet = case.news is NewsFinding.NOTHING and case.candidate and is_stale(case.candidate, self.today)
        if stale_and_quiet and case.candidate.profile_url and Step.REREAD not in case.done:
            gain = self.rates[Step.REREAD].chance * (1.0 - now)
            options.append(Action(Step.REREAD, [case], self.prices.person_enrich(1), gain))
        return [o for o in options if o.gain > 0]


class EnrichEverything(Planner):
    """The naive buyer the race compares against.

    For every company, in order: search its people, buy its full company
    record, then buy the full record of whoever it found. It never checks the
    news, so it can't catch someone who has left. Simple, and it spends on
    records that weren't needed.
    """

    def next_action(self, cases: list[Case], available: float) -> Action | None:
        for case in cases:
            for step, cost in ((Step.SEARCH, self.prices.person_search(RESULTS_PER_COMPANY)),
                               (Step.LEADERS, self.prices.company_enrich(1)),
                               (Step.REREAD, self.prices.person_enrich(1))):
                if step in case.done or step in self.blocked:
                    continue
                if step is Step.REREAD and not (case.candidate and case.candidate.profile_url):
                    continue
                if cost > available:
                    return None
                return Action(step, [case], cost, gain=0.0)
        return None

    def learn(self, action: Action, values_before: list[float]) -> None:
        """The naive buyer doesn't learn."""
