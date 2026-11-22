"""What the records say, reduced to what checking a deck needs.

`tally/crust/schema.py` builds these from Crustdata's JSON; `claims.py` judges deck claims against them.
Every piece carries the purchase that bought it and the date of the record, for the receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Employment:
    employer: str
    title: str | None = None
    start: str | None = None
    end: str | None = None

    def describe(self) -> str:
        span = f"{(self.start or '?')[:7]} to {(self.end or 'now')[:7]}"
        return f"{self.employer}" + (f", {self.title}" if self.title else "") + f" ({span})"


@dataclass
class PersonRecord:
    name: str
    person_id: int | None = None
    title: str | None = None
    profile_url: str | None = None
    past: list[Employment] = field(default_factory=list)
    schools: list[str] = field(default_factory=list)
    updated_at: str | None = None
    call_key: str | None = None  # the purchase it came from
    ref: str | None = None  # profile URL or request id


@dataclass
class Round:
    type: str | None
    amount_usd: float | None
    date: str | None
    investors: list[str] = field(default_factory=list)


@dataclass
class CompanyProfile:
    """One Company Enrich record."""

    company_id: int
    name: str
    domain: str | None = None
    domains: list[str] = field(default_factory=list)
    headcount: int | None = None
    headcount_date: str | None = None
    headcount_series: list[tuple[str, int]] = field(default_factory=list)  # (date, people), oldest first
    growth_12m_pct: float | None = None
    funding_total_usd: float | None = None
    last_round_type: str | None = None
    last_round_amount_usd: float | None = None
    last_round_date: str | None = None
    rounds: list[Round] = field(default_factory=list)
    investors: list[str] = field(default_factory=list)  # the company-level investor list
    founders: list[PersonRecord] = field(default_factory=list)
    leaders: list[PersonRecord] = field(default_factory=list)  # CXOs and decision makers
    competitor_ids: list[int] = field(default_factory=list)
    competitor_domains: list[str] = field(default_factory=list)
    openings: int | None = None  # open jobs, as of `record_date`
    year_founded: int | None = None
    city: str | None = None
    country: str | None = None
    headquarters: str | None = None
    revenue_low_usd: float | None = None
    revenue_high_usd: float | None = None
    record_date: str | None = None
    call_key: str | None = None
    ref: str | None = None

    def all_investors(self) -> list[str]:
        names = list(self.investors)
        for r in self.rounds:
            names.extend(r.investors)
        return list(dict.fromkeys(n for n in names if n))

    def people(self) -> list[PersonRecord]:
        return self.founders + [p for p in self.leaders if p.person_id not in {f.person_id for f in self.founders}]


@dataclass
class JobPost:
    title: str
    date: str | None
    url: str | None = None
    description: str = ""


@dataclass
class JobPosts:
    posts: list[JobPost]
    total: int
    call_key: str | None = None
    ref: str | None = None


@dataclass
class WebFinding:
    results: list[dict]  # title, url, snippet
    call_key: str | None = None


@dataclass
class Comparable:
    name: str
    domain: str | None
    headcount: int | None
    funding_total_usd: float | None
    last_round_type: str | None
    last_round_date: str | None
    record_date: str | None
    company_id: int | None = None
