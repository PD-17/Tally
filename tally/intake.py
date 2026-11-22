"""Reading the client's job: the model first, these rules when it is unavailable."""

from __future__ import annotations

import re

from tally.llm import Brain
from tally.models import JobSpec

HEAD_OF_ENGINEERING_TITLES = [
    "Head of Engineering", "VP Engineering", "VP of Engineering", "Vice President Engineering",
    "Director of Engineering", "CTO", "Chief Technology Officer", "Head of Technology", "Engineering Manager",
]
COUNTRIES = {"india": "India", "indian": "India", "singapore": "Singapore", "us": "United States",
             "american": "United States", "uk": "United Kingdom", "british": "United Kingdom"}
INDUSTRIES = {"fintech": "Financial Services", "financial": "Financial Services", "saas": "Software Development",
              "software": "Software Development", "healthtech": "Hospitals and Health Care"}
DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|in|io|co|ai|org|net)\b")


async def read_job(job_text: str, brain: Brain) -> JobSpec:
    spec = await brain.read_job(job_text) if brain.enabled else None
    return spec or read_job_with_rules(job_text)


def read_job_with_rules(job_text: str) -> JobSpec:
    """A plain reading of requests like '... head of engineering at 60 Indian fintech companies with 50-500 people'."""
    text = job_text.lower()
    words = set(re.findall(r"[a-z]+", text))

    count = re.search(r"(\d+)\s+(?:[a-z-]+\s+){0,3}(?:companies|firms|startups)", text)
    size = re.search(r"(\d+)\s*(?:-|–|to)\s*(\d+)\s*(?:people|employees|staff)", text)
    domains = DOMAIN.findall(text)
    country = next((COUNTRIES[w] for w in words if w in COUNTRIES), None)
    industry = next((INDUSTRIES[w] for w in words if w in INDUSTRIES), None)

    wants_engineering_lead = any(p in text for p in ("head of engineering", "engineering lead", "vp engineering",
                                                     "cto", "engineering leader"))
    unsupported = None if wants_engineering_lead else (
        "This service finds a named role (such as the head of engineering) at a set of companies; "
        "it can't do this request as written.")

    return JobSpec(
        role="head of engineering",
        role_titles=HEAD_OF_ENGINEERING_TITLES,
        country=country,
        industry=industry,
        min_headcount=int(size.group(1)) if size else None,
        max_headcount=int(size.group(2)) if size else None,
        company_count=int(count.group(1)) if count else max(len(domains), 10),
        company_domains=domains,
        unsupported_reason=unsupported,
    )
