"""Turning Crustdata's JSON into Tally's data, in one place.

Field names follow docs.crustdata.com (API 2025-11-01). The docs disagree in
places (for example `current` is sometimes an object and sometimes a list),
so every reader here accepts both. If a live response looks different on the
day, this is the only file to change.
"""

from __future__ import annotations

from typing import Any

from tally.models import Candidate, Company


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


def candidate_from_profile(profile: dict) -> Candidate:
    """A person-search profile or person-enrich record, reduced to what the job needs.

    Emails, phone numbers and everything else are deliberately left behind.
    """
    role = current_role(profile)
    return Candidate(
        person_id=profile.get("crustdata_person_id"),
        name=dig(profile, "basic_profile.name", ""),
        title=role.get("title") or dig(profile, "basic_profile.current_title", ""),
        company_id=role.get("crustdata_company_id") or role.get("company_id"),
        start_date=_date_only(role.get("start_date")),
        profile_url=dig(profile, "social_handles.professional_network_identifier.profile_url"),
        updated_at=dig(profile, "metadata.updated_at") or profile.get("updated_at")
        or dig(profile, "basic_profile.last_updated"),
    )


def company_from_record(record: dict) -> Company:
    """A company-search result or an identify/enrich `company_data`."""
    info = record.get("basic_info", {})
    company_id = record.get("crustdata_company_id") or info.get("crustdata_company_id")
    domain = info.get("primary_domain")
    aliases = {a.lower() for a in [info.get("name"), domain, *info.get("all_domains", [])] if a}
    return Company(company_id=int(company_id), name=info.get("name", f"company {company_id}"),
                   domain=domain, headcount=dig(record, "headcount.total"), aliases=aliases)


def identified(body: list[dict]) -> list[tuple[str, Company | None]]:
    """Company Identify answers: (what was asked, the best match or None)."""
    results = []
    for entry in body or []:
        matches = sorted(entry.get("matches") or [], key=lambda m: m.get("confidence_score", 0), reverse=True)
        best = matches[0]["company_data"] if matches else None
        results.append((str(entry.get("matched_on")), company_from_record(best) if best else None))
    return results


def enriched_leaders(body: list[dict]) -> dict[int, list[Candidate]]:
    """Company Enrich `people` groups: company id -> its founders, CXOs and decision makers."""
    leaders: dict[int, list[Candidate]] = {}
    for entry in body or []:
        for match in entry.get("matches") or []:
            data = match.get("company_data") or {}
            company_id = data.get("crustdata_company_id")
            people = data.get("people") or {}
            seen, found = set(), []
            for group in ("cxos", "decision_makers", "founders"):
                for person in people.get(group) or []:
                    pid = person.get("crustdata_person_id")
                    if pid in seen:
                        continue
                    seen.add(pid)
                    found.append(Candidate(
                        person_id=pid, name=dig(person, "basic_profile.name", ""),
                        title=dig(person, "basic_profile.current_title", ""), company_id=company_id,
                        start_date=None, profile_url=None, updated_at=dig(data, "metadata.updated_at"),
                    ))
            if company_id is not None:
                leaders[int(company_id)] = found
    return leaders


def enriched_person(body: list[dict]) -> Candidate | None:
    """Person Enrich: the matched person, or None when not found."""
    for entry in body or []:
        for match in entry.get("matches") or []:
            if match.get("person_data"):
                return candidate_from_profile(match["person_data"])
    return None


def web_results(body: dict) -> list[dict]:
    """Web Search results: title, url and snippet (the docs show no published date)."""
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("snippet", "")}
            for r in (body or {}).get("results", []) if r.get("url")]


def _date_only(value: str | None) -> str | None:
    return value[:10] if value else None
