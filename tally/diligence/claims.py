"""Judging each deck claim against the records. Fixed rules decide; no model, and nothing in the deck.

Decks round up more than they lie, so every number has a tolerance. And a missing record is
not a contradiction: only a record that is there and says otherwise can contradict a claim.
A contradiction that one more check might overturn is marked `provisional`, so the planner
knows that check is worth buying before the memo calls a founder wrong.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from tally.diligence.deck import ClaimKind, DeckClaim
from tally.diligence.evidence import CompanyProfile, JobPosts, PersonRecord, WebFinding
from tally.models import Source

# Tolerances, in one place.
HEADCOUNT_ROUND_UP = 1.25  # a deck may claim up to 25% (+5 people) over the record and still be "confirmed"
HEADCOUNT_SLACK = 5
HEADCOUNT_CONTRADICT = 1.5  # at 50% over the record (and 10+ people), the record contradicts it
HEADCOUNT_CONTRADICT_MIN_GAP = 10
MONEY_TOLERANCE = 0.2  # rounding and exchange rates
MONEY_CONTRADICT = 1.6
GROWTH_TOLERANCE = 0.35
HIRING_SHARE = 0.5  # open posts covering half the claimed hiring is enough
FOUNDED_SLACK = 1
THIN_HISTORY = 2  # fewer past jobs than this and a missing employer proves nothing
FUNDING_WORDS = ("invest", "led", "round", "raise", "raised", "funding", "backed", "seed", "series")


class Verdict(str, Enum):
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    RECORDS_DISAGREE = "records_disagree"  # two records disagree with each other: both shown, with dates
    CANT_VERIFY = "cant_verify"
    HELD = "held"  # a source timed out three times
    NOT_CHECKED = "not_checked"  # the budget or time ran out before this claim was checked


@dataclass
class Judgement:
    verdict: Verdict
    reason: str
    sources: list[Source] = field(default_factory=list)
    provisional: bool = False  # a check not yet bought could still change this


@dataclass
class Evidence:
    """Everything bought so far for one deck."""

    profile: CompanyProfile | None = None
    jobs: JobPosts | None = None
    people: dict[str, PersonRecord] = field(default_factory=dict)  # bought person records, by normalized name
    web: dict[int, WebFinding] = field(default_factory=dict)  # web checks, by claim index
    inr_per_usd: float | None = None


# ----- matching names --------------------------------------------------------------

NAME_NOISE = {"ventures", "venture", "capital", "partners", "partner", "fund", "funds", "india", "llp", "llc", "inc",
              "ltd", "limited", "pvt", "private", "advisors", "advisers", "investments", "holdings", "management",
              "the", "and", "co", "group", "technologies", "technology", "labs", "corporation", "corp"}
EMPLOYER_ALIASES = {"google": {"google", "alphabet", "deepmind"}, "meta": {"meta", "facebook", "instagram"},
                    "amazon": {"amazon", "aws", "amazon web services"}, "microsoft": {"microsoft", "linkedin"},
                    "flipkart": {"flipkart", "myntra"}, "peak xv": {"peak xv", "sequoia"}}


def normalize_name(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower())
    core = [w for w in words if w not in NAME_NOISE]
    return " ".join(core or words)


def names_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    for group in EMPLOYER_ALIASES.values():
        if any(_has_phrase(na, g) for g in group) and any(_has_phrase(nb, g) for g in group):
            return True
    shorter, longer = sorted((na, nb), key=len)
    return len(shorter) >= 4 and _has_phrase(longer, shorter)


def normalize_school(name: str) -> str:
    s = name.lower()
    s = re.sub(r"indian institute of technology", "iit", s)
    s = re.sub(r"indian institute of management", "iim", s)
    s = re.sub(r"birla institute of technology (?:and|&) science", "bits", s)
    s = re.sub(r"indian institute of science", "iisc", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(iit|iim)\s*-?\s*", r"\1 ", s)
    return " ".join(w for w in s.split() if w not in ("the", "of", "university", "college"))


def schools_match(a: str, b: str) -> bool:
    na, nb = normalize_school(a), normalize_school(b)
    shorter, longer = sorted((na, nb), key=len)
    return bool(shorter) and (na == nb or _has_phrase(longer, shorter))


def person_key(name: str) -> str:
    return " ".join(re.findall(r"[a-z]+", name.lower()))


def find_person(name: str | None, evidence: Evidence) -> PersonRecord | None:
    """The best record for this founder: a bought person record, else the company record's own."""
    if not name:
        return None
    bought = evidence.people.get(person_key(name))
    if bought and (bought.person_id or bought.past or bought.schools or bought.title):
        return bought  # (a bought search that found nobody leaves an empty record: fall through)
    if evidence.profile:
        for person in evidence.profile.people():
            if _same_person(name, person.name):
                return person
    return None


def _same_person(a: str, b: str) -> bool:
    pa, pb = person_key(a).split(), person_key(b).split()
    return bool(pa and pb) and pa[0] == pb[0] and pa[-1] == pb[-1]


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


# ----- the rules ---------------------------------------------------------------------

def judge(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    """This claim's verdict from what has been bought so far."""
    rule = RULES.get(claim.kind, _web_only)
    return rule(claim, index, ev)


def _profile_source(ev: Evidence, field_name: str, value) -> Source:
    p = ev.profile
    return Source("crustdata_record", p.ref or f"company {p.company_id}", field_name, str(value),
                  p.record_date, p.call_key)


def _not_checked(why: str = "the company record wasn't bought") -> Judgement:
    return Judgement(Verdict.NOT_CHECKED, f"not checked at this budget ({why})")


def headcount(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    p = ev.profile
    if p is None:
        return _not_checked()
    if p.headcount is None or claim.number is None:
        return Judgement(Verdict.CANT_VERIFY, "no headcount on record")
    n, rec = claim.number, p.headcount
    record = Source("crustdata_record", p.ref or "", "headcount.total", f"{rec} people", p.headcount_date or
                    p.record_date, p.call_key)
    dated = f"{rec} people as of {(p.headcount_date or p.record_date or 'an unknown date')[:10]}"
    if claim.qualifier == "at_least":
        fits = rec >= n / HEADCOUNT_ROUND_UP - HEADCOUNT_SLACK
    elif claim.qualifier == "at_most":
        fits = rec <= n * HEADCOUNT_ROUND_UP + HEADCOUNT_SLACK
    else:
        fits = 0.6 * rec <= n <= rec * HEADCOUNT_ROUND_UP + HEADCOUNT_SLACK
    if fits:
        rounded = " (the deck rounds up)" if n > rec else ""
        return Judgement(Verdict.CONFIRMED, f"the record shows {dated}{rounded}", [record])
    if n >= rec * HEADCOUNT_CONTRADICT and n - rec >= HEADCOUNT_CONTRADICT_MIN_GAP:
        stated = _team_size_in_posts(ev, after=p.headcount_date or p.record_date, near=n)
        if stated:
            post, size = stated
            job = Source("job_post", post.url or "", "description", f"team of {size}", post.date, ev.jobs.call_key)
            return Judgement(Verdict.RECORDS_DISAGREE,
                             f"the headcount record shows {dated}, but a job post dated {(post.date or '?')[:10]} "
                             f"says the team is {size}: both shown", [record, job])
        newer = _posts_after(ev, p.headcount_date or p.record_date)
        note = f"; {len(newer)} roles posted since then" if newer else ""
        sources = [record] + ([Source("job_post", newer[0].url or "", "posted", newer[0].title, newer[0].date,
                                      ev.jobs.call_key)] if newer else [])
        return Judgement(Verdict.CONTRADICTED, f"the deck says {n:g}; the record shows {dated}{note}", sources,
                         provisional=ev.jobs is None)
    return Judgement(Verdict.CANT_VERIFY, f"the deck says {n:g}; the record shows {dated}, outside the rounding "
                                          f"tolerance but not far enough to call it wrong", [record])


def headcount_growth(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    p = ev.profile
    if p is None:
        return _not_checked()
    actual = _growth_multiple(p)
    claimed = claimed_multiple(claim)
    if actual is None or claimed is None:
        return Judgement(Verdict.CANT_VERIFY, "no headcount history for the last 12 months on record")
    shown = f"{(actual - 1) * 100:+.0f}% in 12 months ({actual:.2f}x)"
    src = _profile_source(ev, "headcount.growth_percent.yoy", shown)
    said = f"{(claimed - 1) * 100:+.0f}%"
    if abs((actual - 1) - (claimed - 1)) <= GROWTH_TOLERANCE * abs(claimed - 1) + 0.03:
        return Judgement(Verdict.CONFIRMED, f"the record shows {shown}", [src])
    if actual - 1 < (claimed - 1) / 2:
        return Judgement(Verdict.CONTRADICTED, f"the deck says {said}; the record shows {shown}", [src])
    return Judgement(Verdict.CANT_VERIFY, f"the deck says {said}; the record shows {shown}", [src])


def claimed_multiple(claim: DeckClaim) -> float | None:
    """"+31%" and "1.31x" and "grew 3x" as a multiple (1.31, 1.31, 3)."""
    if claim.number is None:
        return None
    n = claim.number
    if re.search(r"\d\s*%|percent", claim.text, re.I):
        return 1 + n / 100 if n >= 2 else n  # 31 (percent) or 1.31 (already a multiple)
    if re.search(r"\d\s*x\b", claim.text, re.I):
        return n
    return 1 + n / 100 if n > 5 else n


def funding_total(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    p = ev.profile
    if p is None:
        return _not_checked()
    claimed = _usd(claim, ev)
    if claimed is None:
        return Judgement(Verdict.CANT_VERIFY, "the amount can't be compared (no exchange rate set)")
    web = _funding_news(ev, index, p.name)
    if not p.funding_total_usd:
        if web:
            return Judgement(Verdict.CONFIRMED, "no funding record, but a news page reports the raise", [web])
        return Judgement(Verdict.CANT_VERIFY, "no funding on record", provisional=index not in ev.web)
    rec = p.funding_total_usd
    src = _profile_source(ev, "funding.total_investment_usd", _money(rec))
    if abs(claimed - rec) <= MONEY_TOLERANCE * rec:
        return Judgement(Verdict.CONFIRMED, f"the funding record totals {_money(rec)}", [src])
    if claimed < rec:
        return Judgement(Verdict.CANT_VERIFY, f"the record totals more ({_money(rec)}); the deck may count one "
                                              f"round", [src])
    if claimed >= rec * MONEY_CONTRADICT:
        if web:
            return Judgement(Verdict.RECORDS_DISAGREE, f"the funding record totals {_money(rec)}, but a news page "
                                                       f"reports a raise: both shown", [src, web])
        return Judgement(Verdict.CONTRADICTED, f"the deck says {_money(claimed)}; the funding record totals "
                                               f"{_money(rec)} (last round {_round_line(p)})", [src],
                         provisional=index not in ev.web)
    return Judgement(Verdict.CANT_VERIFY, f"the record totals {_money(rec)}: above the tolerance but not far "
                                          f"enough to call it wrong", [src])


def last_round(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    p = ev.profile
    if p is None:
        return _not_checked()
    wanted = _round_type(claim.subject)
    rounds = [r for r in p.rounds if r.type] or ([] if not p.last_round_type else
                                                 [_RoundLike(p.last_round_type, p.last_round_amount_usd,
                                                             p.last_round_date)])
    web = _funding_news(ev, index, p.name, also=claim.subject)
    if not rounds:
        if web:
            return Judgement(Verdict.CONFIRMED, "no rounds on record, but a news page reports this round", [web])
        return Judgement(Verdict.CANT_VERIFY, "no funding rounds on record", provisional=index not in ev.web)
    match = next((r for r in rounds if _round_type(r.type) == wanted), None) if wanted else None
    if match is None and wanted:
        listed = ", ".join(f"{round_label(r.type)} ({(r.date or '?')[:10]})" for r in rounds)
        src = _profile_source(ev, "funding.milestones", listed)
        if web:
            return Judgement(Verdict.RECORDS_DISAGREE, f"the funding record lists {listed}, but a news page reports "
                                                       f"the {claim.subject}: both shown", [src, web])
        return Judgement(Verdict.CONTRADICTED, f"no {claim.subject} on record; the rounds on record are {listed}",
                         [src], provisional=index not in ev.web)
    match = match or rounds[0]
    src = _profile_source(ev, "funding.milestones", f"{round_label(match.type)}, {_money(match.amount_usd)}, "
                                                    f"{(match.date or '?')[:10]}")
    claimed = _usd(claim, ev)
    if claimed and match.amount_usd and claimed >= match.amount_usd * MONEY_CONTRADICT:
        return Judgement(Verdict.CONTRADICTED, f"the deck says {_money(claimed)}; the {match.type} on record was "
                                               f"{_money(match.amount_usd)}", [src])
    if claimed and match.amount_usd and abs(claimed - match.amount_usd) > MONEY_TOLERANCE * match.amount_usd:
        return Judgement(Verdict.CANT_VERIFY, f"the {match.type} is on record at {_money(match.amount_usd)}", [src])
    return Judgement(Verdict.CONFIRMED, f"{round_label(match.type)} on record, dated {(match.date or '?')[:10]}", [src])


def investor(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    p = ev.profile
    if p is None:
        return _not_checked()
    for r in p.rounds:
        for name in r.investors:
            if names_match(claim.subject, name):
                src = _profile_source(ev, "funding.milestones.investors", f"{name}: {round_label(r.type)}, "
                                                                          f"{(r.date or '?')[:10]}")
                return Judgement(Verdict.CONFIRMED, f"{name} is on record in the {round_label(r.type)} "
                                                    f"({(r.date or '?')[:10]})", [src])
    for name in p.investors:
        if names_match(claim.subject, name):
            return Judgement(Verdict.CONFIRMED, f"{name} is on the company's investor list",
                             [_profile_source(ev, "funding.investors", name)])
    web = _web_naming(ev, index, [claim.subject or "", p.name], FUNDING_WORDS)
    if web:
        return Judgement(Verdict.CONFIRMED, f"not in the funding record, but a page names {claim.subject} "
                                            f"investing in {p.name}", [web])
    on_record = p.all_investors()
    if not on_record:
        return Judgement(Verdict.CANT_VERIFY, "no investors on record", provisional=index not in ev.web)
    listed = ", ".join(on_record[:8])
    src = _profile_source(ev, "funding.milestones.investors", listed)
    checked = " and a web search found nothing" if index in ev.web else ""
    return Judgement(Verdict.CONTRADICTED, f"{claim.subject} isn't among the investors on record ({listed}){checked}",
                     [src], provisional=index not in ev.web)


def founded(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    p = ev.profile
    if p is None:
        return _not_checked()
    if not p.year_founded or claim.number is None:
        return Judgement(Verdict.CANT_VERIFY, "no founding year on record")
    src = _profile_source(ev, "basic_info.year_founded", p.year_founded)
    if abs(p.year_founded - claim.number) <= FOUNDED_SLACK:
        return Judgement(Verdict.CONFIRMED, f"founded {p.year_founded} on record", [src])
    return Judgement(Verdict.CONTRADICTED, f"the deck says {claim.number:.0f}; the record says {p.year_founded}",
                     [src])


def founder_role(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    if ev.profile is None:
        return _not_checked()
    person = find_person(claim.person, ev)
    if person is None:
        return Judgement(Verdict.CANT_VERIFY, f"{claim.person} isn't among the founders or leaders on the company "
                                              f"record", provisional=person_key(claim.person or "") not in ev.people)
    src = _person_source(person, "current title", person.title or "listed as a founder or leader")
    return Judgement(Verdict.CONFIRMED, f"{person.name} is on record as {person.title or 'a founder or leader'}",
                     [src])


def founder_past_employer(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    if ev.profile is None:
        return _not_checked()
    person = find_person(claim.person, ev)
    bought = person_key(claim.person or "") in ev.people
    if person is None or not person.past:
        who = claim.person or "this founder"
        return Judgement(Verdict.CANT_VERIFY, f"no work history on record for {who}", provisional=not bought)
    for job in person.past:
        if names_match(claim.subject, job.employer):
            return Judgement(Verdict.CONFIRMED, f"{person.name}: {job.describe()}",
                             [_person_source(person, "experience.past", job.describe())])
    history = "; ".join(j.describe() for j in person.past[:6])
    src = _person_source(person, "experience.past", history)
    if len(person.past) >= THIN_HISTORY:
        return Judgement(Verdict.CONTRADICTED, f"{claim.subject} isn't in {person.name}'s work history on record "
                                               f"({history})", [src])
    return Judgement(Verdict.CANT_VERIFY, f"{person.name}'s history on record is thin ({history})", [src],
                     provisional=not bought)


def founder_education(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    if ev.profile is None:
        return _not_checked()
    person = find_person(claim.person, ev)
    bought = person_key(claim.person or "") in ev.people
    if person is None or not person.schools:
        return Judgement(Verdict.CANT_VERIFY, f"no education on record for {claim.person or 'this founder'}",
                         provisional=not bought)
    for school in person.schools:
        if schools_match(claim.subject or "", school):
            return Judgement(Verdict.CONFIRMED, f"{person.name}: {school}",
                             [_person_source(person, "education.schools", school)])
    listed = "; ".join(person.schools)
    return Judgement(Verdict.CONTRADICTED, f"{claim.subject} isn't in {person.name}'s education on record "
                                           f"({listed})", [_person_source(person, "education.schools", listed)])


def hiring(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    p = ev.profile
    if p is None:
        return _not_checked()
    need = HIRING_SHARE * (claim.number or 1)
    if ev.jobs is not None:
        jobs = Source("job_search", ev.jobs.ref or "", "open posts", f"{ev.jobs.total} posts",
                      ev.jobs.posts[0].date if ev.jobs.posts else None, ev.jobs.call_key)
        if ev.jobs.total >= need:
            return Judgement(Verdict.CONFIRMED, f"{ev.jobs.total} job posts on record", [jobs])
        if p.openings is not None and p.openings >= need:
            src = _profile_source(ev, "hiring.openings_count", p.openings)
            return Judgement(Verdict.RECORDS_DISAGREE, f"the company record counts {p.openings} openings, but the "
                                                       f"job search finds {ev.jobs.total} posts: both shown",
                             [src, jobs])
        return Judgement(Verdict.CANT_VERIFY, f"only {ev.jobs.total} job posts on record (job posts are often "
                                              f"incomplete, so this isn't a contradiction)", [jobs])
    if p.openings is not None and p.openings >= need:
        return Judgement(Verdict.CONFIRMED, f"the company record counts {p.openings} openings",
                         [_profile_source(ev, "hiring.openings_count", p.openings)])
    return Judgement(Verdict.NOT_CHECKED, "job posts not checked at this budget", provisional=True)


COUNTRY_CODES = {"ind": "india", "in": "india", "usa": "united states", "us": "united states",
                 "united states of america": "united states", "uk": "united kingdom", "gb": "united kingdom",
                 "gbr": "united kingdom", "sgp": "singapore", "sg": "singapore", "are": "uae", "ae": "uae",
                 "united arab emirates": "uae", "deu": "germany", "de": "germany", "idn": "indonesia"}
COUNTRIES = set(COUNTRY_CODES.values()) | {"australia", "canada", "france", "japan", "netherlands"}
PLACE_ALIASES = {"bengaluru": ("bengaluru", "bangalore"), "bangalore": ("bengaluru", "bangalore"),
                 "gurugram": ("gurugram", "gurgaon"), "gurgaon": ("gurugram", "gurgaon"),
                 "mumbai": ("mumbai", "bombay"), "chennai": ("chennai", "madras"),
                 "kolkata": ("kolkata", "calcutta"),
                 "delhi ncr": ("delhi", "new delhi", "gurugram", "gurgaon", "noida", "ncr"),
                 "ncr": ("delhi", "new delhi", "gurugram", "gurgaon", "noida", "ncr")}


def _plain(text: str) -> str:
    """Lower case, accents off: "Karnātaka" -> "karnataka"."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _country(name: str | None) -> str | None:
    if not name:
        return None
    plain = _plain(name).strip()
    return COUNTRY_CODES.get(plain, plain)


def location(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    """Matched against the city, state, country, street and office addresses, and where its people work."""
    p = ev.profile
    if p is None:
        return _not_checked()
    record = [x for x in (p.city, p.headquarters, p.country) if x] + p.addresses
    if not record and not p.regions:
        return Judgement(Verdict.CANT_VERIFY, "no location on record")
    text = _plain(" | ".join(record))
    regions = _plain(" | ".join(p.regions))
    parts = [x.strip() for x in re.split(r",|\band\b|&|/", _plain(claim.subject or "")) if len(x.strip()) >= 3]
    shown = "; ".join(record[:3])
    found_in_address = [x for x in parts if any(_has_phrase(text, a) for a in PLACE_ALIASES.get(x, (x,)))]
    found_in_people = [x for x in parts if x not in found_in_address
                       and any(_has_phrase(regions, a) for a in PLACE_ALIASES.get(x, (x,)))]
    if parts and len(found_in_address) + len(found_in_people) == len(parts):
        sources = [_profile_source(ev, "locations", shown)] if found_in_address else []
        if found_in_people:
            sources.append(_profile_source(ev, "headcount.by_region_absolute", ", ".join(p.regions[:8])))
        return Judgement(Verdict.CONFIRMED, "on record: " + ", ".join(found_in_address + found_in_people), sources)
    said = _country(claim.subject)
    if said in COUNTRIES and _country(p.country) and _country(p.country) != said:
        return Judgement(Verdict.CONTRADICTED, f"the record says {p.country}", [_profile_source(ev, "locations", shown)])
    return Judgement(Verdict.CANT_VERIFY, f"the record says {shown}", [_profile_source(ev, "locations", shown)])


def _web_only(claim: DeckClaim, index: int, ev: Evidence) -> Judgement:
    """Customers, partners, awards, revenue: a page can confirm them; nothing found proves nothing."""
    company = ev.profile.name if ev.profile else None
    context = []
    if claim.kind is ClaimKind.REVENUE and ev.profile and ev.profile.revenue_high_usd:
        p = ev.profile
        context = [_profile_source(ev, "revenue.estimated", f"{_money(p.revenue_low_usd)} to "
                                                             f"{_money(p.revenue_high_usd)} (an estimate)")]
    if index not in ev.web:
        if ev.profile is None:
            return _not_checked()
        return Judgement(Verdict.NOT_CHECKED, "a web check wasn't bought at this budget", context, provisional=True)
    subject = claim.subject if claim.subject and not names_match(claim.subject, company) else None
    number = number_pattern(claim.text)
    if not subject and not number:
        return Judgement(Verdict.CANT_VERIFY, "nothing specific enough to check on the web", context)
    found = _web_naming(ev, index, [subject or "", company or ""], number=number)
    if found:
        return Judgement(Verdict.CONFIRMED, "a web page says so", [found] + context)
    estimate = " (the revenue estimate is shown for context only)" if context else ""
    return Judgement(Verdict.CANT_VERIFY, f"a web search found no page that says so{estimate}", context)


RULES = {
    ClaimKind.HEADCOUNT: headcount, ClaimKind.HEADCOUNT_GROWTH: headcount_growth,
    ClaimKind.FUNDING_TOTAL: funding_total, ClaimKind.LAST_ROUND: last_round, ClaimKind.INVESTOR: investor,
    ClaimKind.FOUNDED: founded, ClaimKind.FOUNDER_ROLE: founder_role,
    ClaimKind.FOUNDER_PAST_EMPLOYER: founder_past_employer, ClaimKind.FOUNDER_EDUCATION: founder_education,
    ClaimKind.HIRING: hiring, ClaimKind.LOCATION: location,
}


# ----- helpers ------------------------------------------------------------------------

@dataclass
class _RoundLike:
    type: str | None
    amount_usd: float | None
    date: str | None


def _round_type(value: str | None) -> str | None:
    if not value:
        return None
    v = value.lower().replace("_", " ").replace("-", " ")
    if m := re.search(r"series\s*([a-h])\b", v):
        return f"series {m[1]}"
    if "pre seed" in v or "preseed" in v:
        return "pre seed"
    if "seed" in v or "angel" in v:
        return "seed"
    return v.strip()


def round_label(value: str | None) -> str:
    return (value or "round").replace("_", " ").title().replace("Series ", "Series ")


def _round_line(p: CompanyProfile) -> str:
    return f"{round_label(p.last_round_type)}, {(p.last_round_date or '?')[:10]}"


def _usd(claim: DeckClaim, ev: Evidence) -> float | None:
    if claim.number is None:
        return None
    if (claim.currency or "USD").upper() == "INR":
        return claim.number / ev.inr_per_usd if ev.inr_per_usd else None
    return claim.number


def _money(usd: float | None) -> str:
    if usd is None:
        return "an unknown amount"
    if usd >= 1e9:
        return f"${usd / 1e9:.1f}B"
    if usd >= 1e6:
        return f"${usd / 1e6:.1f}M"
    if usd >= 1e3:
        return f"${usd / 1e3:.0f}K"
    return f"${usd:.0f}"


NUMBER_UNITS = [("%", r"(?:%|percent| per cent)"), ("percent", r"(?:%|percent| per cent)"),
                ("billion", r"(?:b|bn|billion)\b"), ("bn", r"(?:b|bn|billion)\b"), ("b", r"(?:b|bn|billion)\b"),
                ("million", r"(?:m|mn|million)\b"), ("mn", r"(?:m|mn|million)\b"), ("m", r"(?:m|mn|million)\b"),
                ("crore", r"(?:cr|crore)\b"), ("cr", r"(?:cr|crore)\b"), ("lakh", r"lakhs?\b"),
                ("k", r"(?:k|thousand)\b")]


def number_pattern(text: str) -> str | None:
    """A regex for the first figure in a claim, with its unit: "15M+ decisions" -> 15 (m|mn|million).
    A page confirms a figure only if it states that figure."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(%|percent|billion|bn|million|mn|crore|cr|lakh|[mbk])?(?![\w])?",
                  text.lower())
    if not m:
        return None
    digits = re.escape(m[1].replace(",", ""))
    unit = next((pattern for name, pattern in NUMBER_UNITS if m[2] == name), None)
    return rf"(?<![\d.]){digits}\s*{unit}" if unit else rf"(?<![\d.]){digits}(?![\d.])"


def _short_number(n: float) -> str:
    for size, unit in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if n >= size:
            return f"{n / size:g}"
    return f"{n:g}"


def _growth_multiple(p: CompanyProfile) -> float | None:
    """Headcount growth over the last 12 months (never over the company's whole history)."""
    if p.growth_12m_pct is not None:
        return 1 + p.growth_12m_pct / 100
    if len(p.headcount_series) >= 2:
        last_date, last = p.headcount_series[-1]
        try:
            target = date.fromisoformat(last_date[:10]).toordinal() - 365
        except ValueError:
            return None
        dated = [(abs(date.fromisoformat(d[:10]).toordinal() - target), n) for d, n in p.headcount_series[:-1]
                 if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d[:10])]
        gap, then = min(dated, default=(None, None))
        if gap is not None and gap <= 60 and then:
            return last / then
    return None


def _posts_after(ev: Evidence, date: str | None) -> list:
    if ev.jobs is None:
        return []
    return [post for post in ev.jobs.posts if post.date and (date is None or post.date[:10] > date[:10])]


def _team_size_in_posts(ev: Evidence, after: str | None, near: float):
    """A job post newer than the headcount record that states a team size close to the deck's."""
    for post in _posts_after(ev, after):
        m = re.search(r"team of\s+(?:over |about |~)?(\d[\d,]*)|(\d[\d,]*)\+?[\s-]*(?:people|member team|"
                      r"strong team)", post.description, re.I)
        if m:
            size = int((m[1] or m[2]).replace(",", ""))
            if abs(size - near) <= 0.25 * near + HEADCOUNT_SLACK:
                return post, size
    return None


def _web_naming(ev: Evidence, index: int, terms: list[str], any_of: tuple[str, ...] = (),
                number: str | None = None) -> Source | None:
    finding = ev.web.get(index)
    if finding is None:
        return None
    for result in finding.results:
        text = f"{result.get('title', '')}. {result.get('snippet', '')}".lower()
        # every name and the funding word in one sentence: a page that mentions both somewhere proves nothing
        fragments = re.split(r"(?<=[.!?])\s+|\s+[·|—–]\s+|\n+", text)
        if any(all(_mentions(f, t) for t in terms if t) and (not any_of or any(w in f for w in any_of))
               and (not number or re.search(number, f)) for f in fragments):
            return Source("web_page", result.get("url", ""), "passage", result.get("snippet", ""),
                          result.get("published"), finding.call_key)
    return None


def _mentions(text: str, name: str) -> bool:
    """The whole name as a phrase; or, for long names, every word of it ("Tiger Global Management")."""
    core = normalize_name(name)
    if not core:
        return False
    if _has_phrase(text, core):
        return True
    words = core.split()
    return len(words) > 1 and all(len(w) >= 4 and _has_phrase(text, w) for w in words)


def _funding_news(ev: Evidence, index: int, company: str, also: str | None = None) -> Source | None:
    return _web_naming(ev, index, [company] + ([also] if also else []), FUNDING_WORDS)


def _person_source(person: PersonRecord, field_name: str, value: str) -> Source:
    return Source("crustdata_record", person.ref or person.profile_url or person.name, f"{person.name}: {field_name}",
                  value, person.updated_at, person.call_key)
