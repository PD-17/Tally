"""The data Tally works with, from the client's request to the receipt."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field


class JobSpec(BaseModel):
    """A research job, read from the client's own words."""

    role: str = Field(description="The role to find at each company, e.g. 'head of engineering'")
    role_titles: list[str] = Field(description="Job titles that count as that role, most specific first")
    country: str | None = Field(description="Full country name the companies must be in, e.g. 'India'")
    industry: str | None = Field(description="Industry in LinkedIn's taxonomy, e.g. 'Financial Services'")
    min_headcount: int | None
    max_headcount: int | None
    company_count: int = Field(description="How many companies the client wants covered")
    company_domains: list[str] = Field(description="Domains the client listed, if they gave a list")
    unsupported_reason: str | None = Field(
        description="Set only when this is not a 'find a role at companies' research job")


class Status(str, Enum):
    """How sure we are about one company's answer. Never a guess."""

    CONFIRMED = "confirmed"  # a fresh record, or a newer source agreeing with it
    PROBABLE = "probable"  # the record says so, but it is old or the title is borderline
    CONFLICT = "conflict"  # a newer source disagrees with the record: both are shown
    CANT_VERIFY = "cant_verify"  # no record settles it; the reason says what's missing
    HELD = "held"  # a source timed out; held for a person rather than read as "not found"
    NOT_REACHED = "not_reached"  # the budget or the time limit ran out first


@dataclass
class Company:
    company_id: int
    name: str
    domain: str | None
    headcount: int | None
    aliases: set[str] = field(default_factory=set)  # every name and domain that resolved to this record


@dataclass
class Candidate:
    """A person who might hold the role. Only the fields the job needs are kept."""

    person_id: int | None
    name: str
    title: str
    company_id: int | None
    start_date: str | None
    profile_url: str | None
    updated_at: str | None


@dataclass
class Source:
    """Where a claim comes from: the exact field or passage, and the record's date."""

    kind: str  # crustdata_record | web_page
    ref: str  # request id, profile URL or page URL
    field: str  # which field, or "passage"
    value: str  # what that field or passage says
    record_date: str | None  # when the record was last updated, or the page was published
    call_key: str | None  # the purchase that bought it


@dataclass
class Purchase:
    """A purchase, or this company's share of a batched one, for the receipt."""

    call_key: str
    endpoint: str
    credits: float  # this company's share
    request_id: str | None


@dataclass
class Answer:
    company: Company
    status: Status
    person: Candidate | None = None
    reason: str = ""  # one line a client can read
    sources: list[Source] = field(default_factory=list)
    purchases: list[Purchase] = field(default_factory=list)  # everything spent on this company

    @property
    def credits(self) -> float:
        return sum(p.credits for p in self.purchases)
