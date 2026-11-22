"""Worst-case prices, so the wallet can refuse a purchase before it is made.

Defaults are the list prices in Crustdata's docs. `load()` replaces them with
this key's own prices from `GET /account/endpoints` (a free call). Every
estimate here must be an upper bound: the wallet reserves it, and the real
cost from `X-Credits-Used` is what is finally spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PriceBook:
    person_search_result: float = 0.03
    # Premium groups are billed per result twice: once for filtering on them, once for returning them.
    person_search_premium: float = 0.1  # the "experience" group (employer, title, start date)
    company_search_result: float = 0.03
    company_search_premium: dict[str, float] = field(default_factory=lambda: {"taxonomy": 0.1, "headcount": 0.2})
    identify_match: float = 0.2
    company_enrich_record: float = 2.0
    person_enrich_record: float = 1.0  # base only: contact fields cost more and are never asked for
    web_search_page: float = 1.0
    web_fetch_url: float = 1.0
    job_search_result: float = 0.03

    def person_search(self, limit: int) -> float:
        return limit * (self.person_search_result + 2 * self.person_search_premium)

    def company_search(self, limit: int, premium_groups: list[str]) -> float:
        premium = sum(self.company_search_premium.get(g, 0.2) for g in premium_groups)
        return limit * (self.company_search_result + 2 * premium)

    def company_fields(self, limit: int, returned_groups: list[str]) -> float:
        """A company search filtered on plain fields that returns premium groups: each charged once."""
        return limit * (self.company_search_result + sum(self.company_search_premium.get(g, 0.2)
                                                         for g in returned_groups))

    def job_search(self, limit: int) -> float:
        return limit * self.job_search_result

    def identify(self, identifiers: int) -> float:
        return identifiers * self.identify_match

    def company_enrich(self, records: int) -> float:
        return records * self.company_enrich_record

    def person_enrich(self, records: int) -> float:
        return records * self.person_enrich_record

    def web_search(self) -> float:
        return self.web_search_page

    async def load(self, client) -> None:
        """Use this key's own base prices where the endpoint list gives them.

        A listed price only ever raises an estimate, never lowers it: every price here
        must stay an upper bound, and a lower figure may mean something other than what
        we assume (per result vs per call, say).
        """
        endpoints = (await client.get("/account/endpoints")).body.get("endpoints", [])
        base = {}
        for e in endpoints:
            try:
                base[e["path"]] = float(e["base_credits"])
            except (KeyError, TypeError, ValueError):
                continue

        def higher(path: str, current: float) -> float:
            return max(current, base.get(path, current))

        self.person_search_result = higher("/person/search", self.person_search_result)
        self.company_search_result = higher("/company/search", self.company_search_result)
        self.identify_match = higher("/company/identify", self.identify_match)
        self.company_enrich_record = higher("/company/enrich", self.company_enrich_record)
        self.person_enrich_record = higher("/person/enrich", self.person_enrich_record)
        self.web_search_page = higher("/web/search/live", self.web_search_page)
        self.job_search_result = higher("/job/search", self.job_search_result)
