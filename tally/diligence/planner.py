"""Choosing the next check: the one that settles the most important claims per credit.

The company record (bought for every deck) settles most claims at once. What's left are
smaller checks, each priced at its worst case:

    job posts       hiring claims, and a headcount the record contradicts (0.6)
    a founder       their own record, when the company record's history is thin (1-1.7)
    a web search    customers, awards, and an investor or round missing from the record (1)

A claim's materiality says how much a partner cares (an investor who never invested matters
more than a logo). A check's value = sum over the claims it could change of
materiality x chance it settles them x how unsettled they are. At a small budget this is
the triage: the material claims get checked first, and the rest say "not checked".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from tally.diligence.claims import Evidence, Judgement, Verdict, find_person, person_key
from tally.diligence.deck import ClaimKind, DeckClaim
from tally.prices import PriceBook

MATERIALITY = {
    ClaimKind.INVESTOR: 1.0, ClaimKind.HEADCOUNT: 1.0, ClaimKind.FUNDING_TOTAL: 0.9, ClaimKind.LAST_ROUND: 0.8,
    ClaimKind.FOUNDER_PAST_EMPLOYER: 0.8, ClaimKind.FOUNDER_ROLE: 0.8, ClaimKind.REVENUE: 0.6,
    ClaimKind.CUSTOMER: 0.6, ClaimKind.FOUNDER_EDUCATION: 0.5, ClaimKind.HIRING: 0.5,
    ClaimKind.HEADCOUNT_GROWTH: 0.5, ClaimKind.PARTNER_OR_AWARD: 0.4, ClaimKind.FOUNDED: 0.4,
    ClaimKind.LOCATION: 0.3, ClaimKind.OTHER_METRIC: 0.2,
}
WEB_CHANCE = {ClaimKind.INVESTOR: 0.5, ClaimKind.FUNDING_TOTAL: 0.4, ClaimKind.LAST_ROUND: 0.4,
              ClaimKind.CUSTOMER: 0.5, ClaimKind.PARTNER_OR_AWARD: 0.4, ClaimKind.REVENUE: 0.3,
              ClaimKind.OTHER_METRIC: 0.2}
WEB_ONLY = (ClaimKind.CUSTOMER, ClaimKind.PARTNER_OR_AWARD, ClaimKind.REVENUE, ClaimKind.OTHER_METRIC)
FOUNDER_KINDS = (ClaimKind.FOUNDER_PAST_EMPLOYER, ClaimKind.FOUNDER_EDUCATION, ClaimKind.FOUNDER_ROLE)
SETTLED_VALUE = {Verdict.CONFIRMED: 1.0, Verdict.CONTRADICTED: 1.0, Verdict.RECORDS_DISAGREE: 0.9,
                 Verdict.CANT_VERIFY: 0.6, Verdict.HELD: 0.5, Verdict.NOT_CHECKED: 0.0}


@dataclass
class ClaimCase:
    index: int
    claim: DeckClaim
    judgement: Judgement
    held: str | None = None

    @property
    def settled(self) -> float:
        value = SETTLED_VALUE[self.judgement.verdict]
        return value * (0.5 if self.judgement.provisional else 1.0)


@dataclass(frozen=True)
class Check:
    key: str  # "jobs", "founder:<name>", "web:<claim index>"
    cost: float
    gain: float
    claims: tuple[int, ...]
    why: str

    @property
    def value_per_credit(self) -> float:
        return self.gain / self.cost if self.cost else float("inf")

    def describe(self) -> str:
        return f"{self.why}: ~{self.gain:.2f} claim value for <= {self.cost:.2f} credits"


@dataclass
class DiligencePlanner:
    prices: PriceBook
    today: date
    floor: float = 0.02
    blocked: set[str] = field(default_factory=set)  # checks refused, forbidden or broken: not tried again
    done: set[str] = field(default_factory=set)

    def options(self, cases: list[ClaimCase], ev: Evidence) -> list[Check]:
        if ev.profile is None:
            return []
        checks = []
        jobs_claims = [c for c in cases if c.claim.kind is ClaimKind.HIRING
                       or (c.claim.kind is ClaimKind.HEADCOUNT and c.judgement.provisional)]
        if jobs_claims:
            gain = sum(MATERIALITY[c.claim.kind] * (0.8 if c.claim.kind is ClaimKind.HIRING else 0.4)
                       * (1 - c.settled) for c in jobs_claims)
            checks.append(Check("jobs", self.prices.job_search(20), gain, tuple(c.index for c in jobs_claims),
                                "check the job posts"))
        by_founder: dict[str, list[ClaimCase]] = {}
        for c in cases:
            if c.claim.kind in FOUNDER_KINDS and c.claim.person and c.judgement.provisional:
                by_founder.setdefault(c.claim.person, []).append(c)
        for name, group in by_founder.items():
            known = find_person(name, ev)
            cost = self.prices.person_enrich(1) if known and known.profile_url else \
                self.prices.person_search(3)
            gain = sum(MATERIALITY[c.claim.kind] * 0.7 * (1 - c.settled) for c in group)
            checks.append(Check(f"founder:{person_key(name)}", cost, gain, tuple(c.index for c in group),
                                f"read {name}'s own record"))
        for c in cases:
            web_worthy = c.claim.kind in WEB_CHANCE and (c.judgement.provisional or c.claim.kind in WEB_ONLY)
            if web_worthy and c.index not in ev.web:
                gain = MATERIALITY[c.claim.kind] * WEB_CHANCE[c.claim.kind] * (1 - c.settled)
                if c.judgement.verdict is Verdict.CONTRADICTED:
                    gain *= 2  # calling a founder wrong deserves the extra look
                checks.append(Check(f"web:{c.index}", self.prices.web_search(), gain, (c.index,),
                                    f"search the web for '{(c.claim.subject or c.claim.text)[:40]}'"))
        return [k for k in checks if k.key not in self.done and k.key not in self.blocked and k.gain > 0]

    def next_action(self, cases: list[ClaimCase], ev: Evidence, available: float) -> Check | None:
        worthwhile = [k for k in self.options(cases, ev) if k.cost <= available and k.value_per_credit >= self.floor]
        return max(worthwhile, key=lambda k: k.value_per_credit, default=None)

    def plan(self, cases: list[ClaimCase], ev: Evidence) -> list[Check]:
        """Every worthwhile check in the order it would be bought (for the quote)."""
        options = [k for k in self.options(cases, ev) if k.value_per_credit >= self.floor]
        return sorted(options, key=lambda k: k.value_per_credit, reverse=True)

    def learn(self, *args) -> None:
        """One deck is too small to learn from; the priors stay."""
