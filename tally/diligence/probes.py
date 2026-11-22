"""The purchases a deck check can make. Each goes through `Buyer._buy` (`tally/probes.py`), so the
wallet's ceiling, the ledger, idempotent call keys and crash recovery apply to every one."""

from __future__ import annotations

import re

from tally.crust import schema
from tally.diligence.evidence import CompanyProfile, Comparable, JobPosts, PersonRecord, WebFinding
from tally.models import Company
from tally.probes import Bought, Buyer
from tally.wallet import BudgetNode

PROFILE_FIELDS = ["basic_info", "headcount", "funding", "people", "competitors", "locations", "metadata",
                  "hiring.openings_count", "revenue", "news"]
PERSON_HISTORY_FIELDS = ["basic_profile", "experience", "education", "social_handles", "metadata"]
COMPARABLE_FIELDS = ["crustdata_company_id", "basic_info", "headcount", "funding", "metadata"]
JOB_FIELDS = ["crustdata_job_id", "job_details", "metadata", "content", "company.basic_info"]
JOBS_LIMIT = 20
COMPARABLES = 5


class DeckBuyer(Buyer):
    async def identify_company(self, node: BudgetNode, domain: str | None,
                               name: str | None) -> Bought[tuple[Company | None, Company | None]]:
        """The company behind the deck's domain, and the company behind its name (0.2 per match).
        Asking both is what catches a lookalike domain."""
        # Seen live: one identify call takes one kind of identifier, so ask by domain, then by name.
        results, bought = [], None
        for kind, value in (("domains", domain and domain.lower().removeprefix("www.")), ("names", name)):
            if not value:
                continue
            bought = await self._buy(node, "/company/identify", {kind: [value]}, self.prices.identify(1),
                                     f"identify the company by its {kind[:-1]}")
            results += schema.identified(bought.value)
        by_domain = next((c for asked, c in results if domain and asked.lower() == domain.lower()), None)
        by_name = next((c for asked, c in results if name and asked.lower() == name.lower()), None)
        return Bought((by_domain, by_name), bought.call_key, bought.credits, bought.request_id)

    async def company_profile(self, node: BudgetNode, company: Company) -> Bought[CompanyProfile | None]:
        """Company Enrich: headcount with history, funding rounds and investors, founders with their
        work history, competitors, location (2 credits; no premium fields)."""
        body = {"crustdata_company_ids": [company.company_id], "fields": PROFILE_FIELDS}
        bought = await self._buy(node, "/company/enrich", body, self.prices.company_enrich(1),
                                 f"buy {company.name}'s company record")
        profile = schema.company_profile(bought.value, bought.call_key, bought.request_id)
        return Bought(profile, bought.call_key, bought.credits, bought.request_id)

    async def job_posts(self, node: BudgetNode, company: CompanyProfile) -> Bought[JobPosts]:
        body = {"filters": {"field": "company.basic_info.crustdata_company_id", "type": "in",
                            "value": [company.company_id]},
                "limit": JOBS_LIMIT, "fields": JOB_FIELDS}
        bought = await self._buy(node, "/job/search", body, self.prices.job_search(JOBS_LIMIT),
                                 f"check {company.name}'s job posts")
        return Bought(schema.job_posts(bought.value, bought.call_key, bought.request_id), bought.call_key,
                      bought.credits, bought.request_id)

    async def founder(self, node: BudgetNode, name: str, known: PersonRecord | None,
                      company: CompanyProfile) -> Bought[PersonRecord | None]:
        """A founder's own record: by profile URL when the company record gave one (1 credit), else a
        person search by name at this company only (never by name alone: names are shared)."""
        if known and known.profile_url:
            body = {"professional_network_profile_urls": [known.profile_url], "fields": PERSON_HISTORY_FIELDS}
            bought = await self._buy(node, "/person/enrich", body, self.prices.person_enrich(1),
                                     f"read {name}'s work history")
            record = schema.enriched_person_record(bought.value, bought.call_key, bought.request_id)
            return Bought(record, bought.call_key, bought.credits, bought.request_id)
        body = {"filters": {"op": "and", "conditions": [
                    {"field": "basic_profile.name", "type": "(.)", "value": name},
                    {"op": "or", "conditions": [
                        {"field": "experience.employment_details.current.company_id", "type": "in",
                         "value": [company.company_id]},
                        {"field": "experience.employment_details.past.company_id", "type": "in",
                         "value": [company.company_id]}]}]},
                "limit": 3, "fields": PERSON_HISTORY_FIELDS + ["crustdata_person_id"]}
        bought = await self._buy(node, "/person/search", body, self.prices.person_search(3),
                                 f"find {name} at {company.name}")
        people = [schema.person_record(p, bought.call_key, bought.request_id)
                  for p in bought.value.get("profiles", [])]
        match = next((p for p in people if _same_name(p.name, name)), None)
        return Bought(match, bought.call_key, bought.credits, bought.request_id)

    async def web(self, node: BudgetNode, query: str, why: str) -> Bought[WebFinding]:
        body = {"query": query, "sources": ["news", "web"]}
        bought = await self._buy(node, "/web/search/live", body, self.prices.web_search(), why)
        return Bought(WebFinding(schema.web_results(bought.value), bought.call_key), bought.call_key,
                      bought.credits, bought.request_id)

    async def comparables(self, node: BudgetNode, company: CompanyProfile,
                          industry: str | None) -> Bought[list[Comparable]]:
        """Five comparable companies with dated headcount and funding: the company's own competitor list
        if it has one (cheap: filtered by id), else the same industry, country and size band."""
        if company.competitor_ids:
            ids = sorted(company.competitor_ids)[:COMPARABLES]
            body = {"filters": {"field": "crustdata_company_id", "type": "in", "value": ids},
                    "limit": len(ids), "fields": COMPARABLE_FIELDS}
            cost = self.prices.company_fields(len(ids), ["headcount", "funding"])
            why = f"comparables: {len(ids)} competitors on {company.name}'s record"
        elif company.competitor_domains:
            domains = company.competitor_domains[:8]
            body = {"filters": {"field": "basic_info.primary_domain", "type": "in", "value": domains},
                    "limit": COMPARABLES, "fields": COMPARABLE_FIELDS}
            cost = self.prices.company_fields(COMPARABLES, ["headcount", "funding"])
            why = f"comparables: competitors of {company.name} by domain"
        else:
            size = company.headcount or 50
            conditions = [{"field": "headcount.total", "type": "=>", "value": max(1, size // 3)},
                          {"field": "headcount.total", "type": "=<", "value": size * 3}]
            if company.country:
                conditions.append({"field": "locations.country", "type": "=", "value": company.country})
            if industry:
                conditions.append({"field": "taxonomy.professional_network_industry", "type": "=", "value": industry})
            body = {"filters": {"op": "and", "conditions": conditions}, "limit": COMPARABLES + 1,
                    "fields": COMPARABLE_FIELDS}
            cost = self.prices.company_search(COMPARABLES + 1, ["taxonomy", "headcount", "funding"])
            why = f"comparables: companies like {company.name} by industry, country and size"
        bought = await self._buy(node, "/company/search", body, cost, why)
        found = [c for c in schema.comparables(bought.value) if c.company_id != company.company_id][:COMPARABLES]
        return Bought(found, bought.call_key, bought.credits, bought.request_id)


def _same_name(a: str, b: str) -> bool:
    pa, pb = re.findall(r"[a-z]+", a.lower()), re.findall(r"[a-z]+", b.lower())
    return bool(pa and pb) and pa[0] == pb[0] and pa[-1] == pb[-1]


# ----- lookalike domains -----------------------------------------------------------------

HOMOGLYPHS = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "4": "a", "7": "t"})


def domain_label(domain: str) -> str:
    return domain.lower().removeprefix("www.").split(".")[0]


def looks_alike(a: str, b: str) -> bool:
    """finv0lt.in vs finvolt.in, kredbl00m vs kredbloom, paysetu-in vs paysetu."""
    la, lb = domain_label(a), domain_label(b)
    if la == lb:
        return a.lower() != b.lower()  # same name, different ending: paysetu.co vs paysetu.in
    ga, gb = la.translate(HOMOGLYPHS).replace("rn", "m"), lb.translate(HOMOGLYPHS).replace("rn", "m")
    return ga == gb or _edit_distance(ga.replace("-", ""), gb.replace("-", "")) <= 2


def _edit_distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def domain_check(deck_domain: str | None, by_domain: Company | None,
                 by_name: Company | None) -> tuple[Company | None, str | None]:
    """Which company to check, and a warning when the deck's domain isn't that company's."""
    if by_domain and by_name and by_domain.company_id != by_name.company_id:
        return by_name, (f"the deck's domain {deck_domain} belongs to {by_domain.name}, not {by_name.name} "
                         f"({by_name.domain}). Checked against {by_name.name}.")
    if by_name and not by_domain and deck_domain:
        real = by_name.domain or "?"
        kind = "a lookalike of" if real != "?" and looks_alike(deck_domain, real) else "not"
        return by_name, (f"the deck's domain {deck_domain} is {kind} {by_name.name}'s domain on record ({real}). "
                         f"Checked against {by_name.name} at {real}.")
    return by_domain or by_name, None
