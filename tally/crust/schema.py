"""Turning Crustdata's JSON into Tally's data, in one place.

Field names follow docs.crustdata.com (API 2025-11-01). The docs disagree in
places (for example `current` is sometimes an object and sometimes a list),
so every reader here accepts both. If a live response looks different on the
day, this is the only file to change.
"""

from __future__ import annotations

from typing import Any

from tally.models import Company


def dig(node: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path, e.g. dig(profile, 'basic_profile.name'). Missing parts give `default`."""
    for key in path.split("."):
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def current_role(profile: dict) -> dict:
    """The person's main current job, whether `current` is an object or a list."""
    current = dig(profile, "experience.employment_details.current")
    if isinstance(current, list):
        main = [job for job in current if job.get("is_default")] or current
        return main[0] if main else {}
    return current or {}


def company_from_record(record: dict) -> Company:
    """A company-search result or an identify/enrich `company_data`."""
    info = record.get("basic_info", {})
    company_id = record.get("crustdata_company_id") or info.get("crustdata_company_id")
    domain = info.get("primary_domain")
    aliases = {a.lower() for a in [info.get("name"), domain, *info.get("all_domains", [])] if a}
    headcount = dig(record, "headcount.total") or _range_top(info.get("employee_count_range"))
    return Company(company_id=int(company_id), name=info.get("name", f"company {company_id}"),
                   domain=domain, headcount=headcount, aliases=aliases)


def _range_top(band: str | None) -> int | None:
    """Identify gives only a band such as "11-50" (seen live). Its top end is the safe reading:
    a company counts as small only if even the top of its band is small."""
    if not band:
        return None
    digits = [int(part) for part in band.replace("+", "").replace(",", "").split("-") if part.strip().isdigit()]
    return max(digits) if digits else None


def identified(body: list[dict]) -> list[tuple[str, Company | None]]:
    """Company Identify answers: (what was asked, the best match or None)."""
    results = []
    for entry in body or []:
        matches = sorted(entry.get("matches") or [], key=lambda m: m.get("confidence_score", 0), reverse=True)
        best = matches[0]["company_data"] if matches else None
        results.append((str(entry.get("matched_on")), company_from_record(best) if best else None))
    return results


def web_results(body: dict) -> list[dict]:
    """Web Search results: title, url and snippet (the docs show no published date)."""
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("snippet", "")}
            for r in (body or {}).get("results", []) if r.get("url")]


def _date_only(value: str | None) -> str | None:
    return value[:10] if value else None


# ----- the company profile, founders' history, job posts, comparables -----
# Field names from this key's `/account/endpoints` field list (saved by `tally check`). Readers
# accept the variants seen across endpoints (company-enrich people use employer_name/employee_title;
# person enrich uses name/title).

def company_profile(body: list[dict], call_key: str | None = None, request_id: str | None = None):
    """Company Enrich: the first matched company as a CompanyProfile, or None."""
    from tally.diligence.evidence import CompanyProfile, Round

    data = next((m.get("company_data") for e in body or [] for m in (e.get("matches") or [])
                 if m.get("company_data")), None)
    if not data:
        return None
    info = data.get("basic_info") or {}
    funding = data.get("funding") or {}
    rounds = []
    for m in _as_list(funding.get("milestones")):
        names = [_name_of(i) for i in _as_list(m.get("investors_detailed")) or _as_list(m.get("investors"))]
        names += [_name_of(i) for i in _as_list(m.get("lead_investors"))]
        rounds.append(Round(type=m.get("round_type") or m.get("name"), amount_usd=_float(m.get("amount_usd")),
                            date=_date_only(m.get("date") or m.get("funding_date")),
                            investors=list(dict.fromkeys(n for n in names if n))))
    rounds.sort(key=lambda r: r.date or "")
    series = sorted(((_date_only(p.get("date")) or "", int(p.get("employee_count") or 0))
                     for p in _as_list(dig(data, "headcount.timeseries")) if p.get("employee_count") is not None))
    growth = dig(data, "headcount.growth_percent")
    growth_12m = _float(growth.get("12m")) if isinstance(growth, dict) else None
    people = data.get("people") or {}
    record_date = dig(data, "metadata.updated_at") or data.get("updated_at")
    ref = request_id or info.get("professional_network_url") or f"company {data.get('crustdata_company_id')}"
    profile = CompanyProfile(
        company_id=int(data.get("crustdata_company_id") or info.get("crustdata_company_id")),
        name=info.get("name", ""), domain=info.get("primary_domain"), domains=list(info.get("all_domains") or []),
        headcount=_int(dig(data, "headcount.total")) or _range_top(info.get("employee_count_range")),
        headcount_date=(series[-1][0] if series else None) or _date_only(record_date),
        headcount_series=series, growth_12m_pct=growth_12m,
        funding_total_usd=_float(funding.get("total_investment_usd") or funding.get("funds_total_usd")),
        last_round_type=funding.get("last_round_type"),
        last_round_amount_usd=_float(funding.get("last_round_amount_usd")),
        last_round_date=_date_only(funding.get("last_fundraise_date")),
        rounds=rounds, investors=[_name_of(i) for i in _as_list(funding.get("investors"))],
        founders=[_person_record(p, call_key, ref) for p in _as_list(people.get("founders"))],
        leaders=[_person_record(p, call_key, ref) for group in ("cxos", "decision_makers")
                 for p in _as_list(people.get(group))],
        competitor_ids=[int(i) for i in _as_list(dig(data, "competitors.company_ids")) if str(i).isdigit()],
        competitor_domains=_competitor_domains(data, info),
        openings=_int(dig(data, "hiring.openings_count")),
        year_founded=_int(info.get("year_founded")),
        city=dig(data, "locations.city"), country=dig(data, "locations.country"),
        headquarters=dig(data, "locations.headquarters") if isinstance(dig(data, "locations.headquarters"), str)
        else None,
        revenue_low_usd=_float(dig(data, "revenue.estimated.lower_bound_usd")),
        revenue_high_usd=_float(dig(data, "revenue.estimated.upper_bound_usd")),
        record_date=_date_only(record_date) or (series[-1][0] if series else None), call_key=call_key, ref=ref,
    )
    return profile


def _competitor_domains(data: dict, info: dict) -> list[str]:
    """Competitor domains from the SEO lists (seen live with stray spaces and full URLs)."""
    own = {d.lower() for d in [info.get("primary_domain"), *(info.get("all_domains") or [])] if d}
    out = []
    for group in ("organic_seo", "paid_seo", "websites"):
        for raw in _as_list(dig(data, f"competitors.{group}")):
            domain = str(raw).strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")
            domain = domain.split("/")[0]
            if domain and "." in domain and domain not in own and domain not in out:
                out.append(domain)
    return out


def person_record(profile: dict, call_key: str | None = None, ref: str | None = None):
    """A person-enrich or person-search profile with its work history and schools."""
    return _person_record(profile, call_key, ref)


def enriched_person_record(body: list[dict], call_key: str | None = None, request_id: str | None = None):
    for entry in body or []:
        for match in entry.get("matches") or []:
            if match.get("person_data"):
                return _person_record(match["person_data"], call_key, request_id)
    return None


def job_posts(body: dict, call_key: str | None = None, request_id: str | None = None):
    """Job Search: posts newest first, and how many there are in all."""
    from tally.diligence.evidence import JobPost, JobPosts

    body = body or {}
    items = body.get("jobs") or body.get("results") or body.get("job_listings") or []
    posts = [JobPost(title=dig(j, "job_details.title") or j.get("title", ""),
                     date=_date_only(dig(j, "metadata.date_added") or dig(j, "metadata.date_updated")),
                     url=dig(j, "job_details.url") or j.get("url"),
                     description=dig(j, "content.description") or "") for j in items]
    posts.sort(key=lambda p: p.date or "", reverse=True)
    total = _int(body.get("total_count")) or len(posts)
    return JobPosts(posts, total, call_key, request_id)


def comparables(body: dict) -> list:
    """Company Search results with headcount and funding, for the comparables table."""
    from tally.diligence.evidence import Comparable

    out = []
    for record in (body or {}).get("companies", []):
        info = record.get("basic_info", {})
        funding = record.get("funding") or {}
        out.append(Comparable(
            name=info.get("name", ""), domain=info.get("primary_domain"),
            headcount=_int(dig(record, "headcount.total")) or _range_top(info.get("employee_count_range")),
            funding_total_usd=_float(funding.get("total_investment_usd")),
            last_round_type=funding.get("last_round_type"),
            last_round_date=_date_only(funding.get("last_fundraise_date")),
            record_date=_date_only(dig(record, "metadata.updated_at")),
            company_id=_int(record.get("crustdata_company_id") or info.get("crustdata_company_id"))))
    return out


def _person_record(p: dict, call_key: str | None, ref: str | None):
    from tally.diligence.evidence import Employment, PersonRecord

    past = []
    for job in _as_list(dig(p, "experience.employment_details.past")):
        employer = job.get("employer_name") or job.get("name") or job.get("company_name")
        if employer:
            past.append(Employment(employer=employer, title=job.get("employee_title") or job.get("title"),
                                   start=_date_only(job.get("employee_start_date") or job.get("start_date")),
                                   end=_date_only(job.get("employee_end_date") or job.get("end_date"))))
    schools = [s.get("school") or s.get("name") for s in _as_list(dig(p, "education.schools"))]
    current = current_role(p)
    url = dig(p, "social_handles.professional_network_identifier.profile_url")
    return PersonRecord(
        name=dig(p, "basic_profile.name", "") or dig(p, "professional_network.name", ""),
        person_id=p.get("crustdata_person_id"),
        title=dig(p, "basic_profile.current_title") or current.get("employee_title") or current.get("title"),
        profile_url=url, past=past, schools=[s for s in schools if s],
        updated_at=_date_only(p.get("updated_at") or dig(p, "metadata.updated_at")
                              or dig(p, "basic_profile.last_updated")),
        call_key=call_key, ref=url or ref)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _name_of(investor) -> str:
    return (investor.get("name") if isinstance(investor, dict) else str(investor or "")).strip()


def _float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None
