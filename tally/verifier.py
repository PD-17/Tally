"""Judging candidates: does this title count, is the record fresh, does newer news disagree?

Fixed rules decide; a model is only asked about borderline titles, and if it
is unavailable the rules still decide. A wrong fact costs more than an honest
"I can't verify", so "confirmed" needs a strong title AND a fresh or
corroborated record.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum

from tally.models import Candidate

STALE_AFTER_DAYS = 45  # Crustdata refreshes records every 1 to 30 days; older than this, re-check
SMALL_COMPANY = 150  # below this, a CTO usually is the head of engineering

LEFT_WORDS = ("left", "steps down", "stepped down", "exits", "exited", "departs", "departed", "resigns",
              "resigned", "quits", "former")
JOINED_WORDS = ("appoints", "appointed", "hires", "hired", "joins", "joined", "names", "named", "promoted")


class TitleVerdict(str, Enum):
    STRONG = "strong"  # clearly the role
    BORDERLINE = "borderline"  # could be: a person or the model should look
    NO = "no"


def judge_title(title: str, headcount: int | None) -> TitleVerdict:
    """Is this title the head of engineering? Rules only."""
    t = " " + re.sub(r"[^a-z ]+", " ", title.lower()) + " "
    leads = any(f" {w} " in t for w in ("head", "vp", "vice president", "director", "svp", "evp"))
    engineering = " engineering " in t
    technology = " technology " in t or " tech " in t
    cto = " cto " in t or "chief technology officer" in t
    if leads and engineering:
        return TitleVerdict.STRONG
    if cto:
        small = headcount is not None and headcount < SMALL_COMPANY
        return TitleVerdict.STRONG if small else TitleVerdict.BORDERLINE
    if (leads and technology) or " engineering manager " in t:
        return TitleVerdict.BORDERLINE
    return TitleVerdict.NO


def best_candidate(people: list[Candidate], headcount: int | None) -> tuple[Candidate, TitleVerdict] | None:
    """The strongest match, preferring a strong title, then the most recent start."""
    judged = [(person, judge_title(person.title, headcount)) for person in people]
    judged = [(person, verdict) for person, verdict in judged if verdict is not TitleVerdict.NO]
    if not judged:
        return None

    def rank(item: tuple[Candidate, TitleVerdict]) -> tuple[int, int]:
        person, verdict = item
        strong_first = 0 if verdict is TitleVerdict.STRONG else 1
        newest_first = -_ordinal(person.start_date)
        return strong_first, newest_first

    return min(judged, key=rank)


def days_old(updated_at: str | None, today: date) -> int | None:
    if not updated_at:
        return None
    try:
        return (today - datetime.fromisoformat(updated_at.replace("Z", "+00:00")).date()).days
    except ValueError:
        return None


def is_stale(person: Candidate, today: date) -> bool:
    age = days_old(person.updated_at, today)
    return age is None or age > STALE_AFTER_DAYS


class NewsFinding(str, Enum):
    CONTRADICTS = "contradicts"  # newer news says they left
    CORROBORATES = "corroborates"  # newer news says they were appointed
    NOTHING = "nothing"


def read_news(person: Candidate, company_name: str, results: list[dict]) -> tuple[NewsFinding, dict | None]:
    """Look for this person, named at this company, leaving or joining. Name and company must both appear."""
    surname = person.name.split()[-1].lower() if person.name else ""
    company_word = company_name.split()[0].lower()
    for result in results:
        text = f"{result['title']} {result['snippet']}".lower()
        if surname not in text or company_word not in text:
            continue
        if any(word in text for word in LEFT_WORDS):
            return NewsFinding.CONTRADICTS, result
        if any(word in text for word in JOINED_WORDS):
            return NewsFinding.CORROBORATES, result
    return NewsFinding.NOTHING, None


def _ordinal(value: str | None) -> int:
    """A date as a number for sorting; unknown dates sort as oldest."""
    try:
        return date.fromisoformat(value[:10]).toordinal() if value else 0
    except ValueError:
        return 0
