"""The data shared across Tally: a company, where a claim comes from, and what it cost."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Company:
    company_id: int
    name: str
    domain: str | None
    headcount: int | None
    aliases: set[str] = field(default_factory=set)  # every name and domain that resolved to this record


@dataclass
class Source:
    """Where a claim comes from: the exact field or passage, and the record's date."""

    kind: str  # crustdata_record | web_page | job_post | job_search
    ref: str  # request id, profile URL or page URL
    field: str  # which field, or "passage"
    value: str  # what that field or passage says
    record_date: str | None  # when the record was last updated, or the page was published
    call_key: str | None  # the purchase that bought it


@dataclass
class Purchase:
    """A purchase, or one claim's share of it, for the receipt."""

    call_key: str
    endpoint: str
    credits: float
    request_id: str | None
