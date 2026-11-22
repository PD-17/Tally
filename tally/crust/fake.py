"""An offline stand-in for Crustdata, FOR TESTS ONLY.

The handbook's first rule is that decisions rest on live data, and mock data
is for tests. This module is that mock: a small made-up world of Indian
fintech companies and their engineering leaders, served through
`httpx.MockTransport` with the same paths, request shapes, response shapes,
prices and headers as the real API (per docs.crustdata.com, API 2025-11-01).

The world holds the edge cases the judges will try:

* Ledgerline has two domains, and must stay one company.
* Finvolt has a lookalike, Finvolt Energy, which is a different company.
* Quietpay has no engineering leader on record: the right answer is "can't verify".
* Upitra's head of engineering left in October; the record is older than the news.
* Kredbloom is small (60 people): its CTO is the engineering head.
* Two different people are both called Rahul Sharma.

`inject(path, fault)` makes the next call to a path fail (429, 500, 403 or a timeout).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

PRICES = {  # credits, as in the docs' pricing page
    "person_search_per_result": 0.03,
    "company_search_per_result": 0.03,
    "identify_per_match": 0.2,
    "company_enrich_per_record": 2.0,
    "person_enrich_per_record": 1.0,
    "web_search_per_page": 1.0,
    "web_fetch_per_url": 1.0,
}

# The person-search filter on the employer is called company_id; the record field is crustdata_company_id.
FIELD_ALIASES = {
    "experience.employment_details.current.company_id": "experience.employment_details.current.crustdata_company_id",
}


def _company(cid, name, domains, headcount, country="India", industry="Financial Services"):
    return {
        "crustdata_company_id": cid,
        "basic_info": {"name": name, "primary_domain": domains[0], "all_domains": domains,
                       "website": f"https://{domains[0]}", "employee_count_range": _band(headcount)},
        "headcount": {"total": headcount},
        "locations": {"country": country},
        "taxonomy": {"professional_network_industry": industry},
    }


def _band(n: int) -> str:
    for low, high in ((1, 10), (11, 50), (51, 200), (201, 500), (501, 1000)):
        if n <= high:
            return f"{low}-{high}"
    return "1001+"


def _person(pid, name, slug, company, title, start, updated="2026-09-01T10:00:00+00:00", country="India"):
    return {
        "crustdata_person_id": pid,
        "basic_profile": {"name": name, "current_title": title, "location": {"country": country,
                                                                              "full_location": f"Bengaluru, {country}"}},
        "social_handles": {"professional_network_identifier": {"profile_url": f"https://www.linkedin.com/in/{slug}"}},
        "experience": {"employment_details": {"current": [{
            "name": company["basic_info"]["name"], "title": title, "start_date": start,
            "crustdata_company_id": company["crustdata_company_id"], "is_default": True}], "past": []}},
        "contact": {"business_emails": [f"{slug}@example.test"]},  # present so the verifier can prove it drops it
        "metadata": {"updated_at": updated},
    }


def build_world() -> tuple[list[dict], list[dict], list[dict]]:
    """Companies, people and web pages of the made-up world."""
    c = {
        "paysetu": _company(1001, "Paysetu Technologies", ["paysetu.in"], 240),
        "ledgerline": _company(1002, "Ledgerline", ["ledgerline.in", "ledgerline.com"], 180),
        "finvolt": _company(1003, "Finvolt", ["finvolt.in"], 320),
        "finvolt_energy": _company(1004, "Finvolt Energy", ["finvoltenergy.com"], 150, industry="Renewable Energy"),
        "quietpay": _company(1005, "Quietpay", ["quietpay.in"], 90),
        "upitra": _company(1006, "Upitra", ["upitra.com"], 410),
        "kredbloom": _company(1007, "Kredbloom", ["kredbloom.in"], 60),
        "coinwell": _company(1008, "Coinwell", ["coinwell.in"], 130),
        "taxmitra": _company(1009, "Taxmitra", ["taxmitra.in"], 75),
        "bigbank": _company(1010, "Bharat Megabank Tech", ["megabanktech.in"], 4800),
        "abroad": _company(1011, "Lendloop", ["lendloop.co"], 200, country="Singapore"),
    }
    people = [
        _person(2001, "Ananya Rao", "ananya-rao", c["paysetu"], "Head of Engineering", "2025-03-01"),
        _person(2002, "Rahul Sharma", "rahul-sharma-paysetu", c["paysetu"], "Senior Software Engineer", "2024-06-01"),
        _person(2003, "Vikram Iyer", "vikram-iyer", c["ledgerline"], "VP Engineering", "2026-09-15",
                updated="2026-10-01T09:00:00+00:00"),
        _person(2004, "Meera Kulkarni", "meera-kulkarni", c["finvolt"], "Director of Engineering", "2023-11-01"),
        _person(2005, "Rahul Sharma", "rahul-sharma-finvolt-energy", c["finvolt_energy"], "Head of Engineering",
                "2022-01-01"),
        _person(2006, "Sana Qureshi", "sana-qureshi", c["quietpay"], "Product Manager", "2024-02-01"),
        _person(2007, "Divya Menon", "divya-menon", c["upitra"], "Head of Engineering", "2021-05-01",
                updated="2026-09-01T10:00:00+00:00"),
        _person(2008, "Arjun Nair", "arjun-nair", c["kredbloom"], "Co-founder & CTO", "2022-08-01"),
        _person(2009, "Kabir Shah", "kabir-shah", c["coinwell"], "Engineering Manager", "2025-01-01"),
        _person(2010, "Ishita Bose", "ishita-bose", c["coinwell"], "VP of Engineering", "2026-10-05",
                updated="2026-10-20T08:00:00+00:00"),
        _person(2011, "Farhan Ali", "farhan-ali", c["taxmitra"], "Head - Technology", "2024-04-01"),
        _person(2012, "Neha Gupta", "neha-gupta", c["bigbank"], "Head of Engineering", "2020-01-01"),
    ]
    pages = [
        {"url": "https://news.example.test/upitra-engineering-head-exits",
         "title": "Upitra's head of engineering Divya Menon steps down",
         "snippet": "Divya Menon, who led engineering at Upitra since 2021, left the company on 20 October 2026.",
         "content": "<html><body><p>Divya Menon, who led engineering at Upitra since 2021, left the company on "
                    "20 October 2026. The company has not named a successor.</p></body></html>",
         "published": "2026-10-21"},
        {"url": "https://news.example.test/ledgerline-hires-vp-engineering",
         "title": "Ledgerline hires Vikram Iyer as VP Engineering",
         "snippet": "Ledgerline (ledgerline.com) appointed Vikram Iyer as VP Engineering in September 2026.",
         "content": "<html><body><p>Ledgerline appointed Vikram Iyer as VP Engineering in September 2026."
                    "</p></body></html>",
         "published": "2026-09-16"},
    ]
    return list(c.values()), people, pages


class SimulatedCrash(BaseException):
    """Stands in for the process being killed. Not an Exception, so nothing catches it on the way out."""


@dataclass
class FakeCrustdata:
    """The made-up Crustdata. Use `.transport()` with `CrustClient(transport=...)`."""

    balance: float = 15_000.0
    companies: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)
    _faults: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self):
        if not self.companies:
            self.companies, self.people, self.pages = build_world()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def inject(self, path: str, fault: str, times: int = 1) -> None:
        """Make the next `times` calls to `path` fail with `fault`: 429, 500, 403, timeout, or
        crash_after_charge (Crustdata charges, then the process dies before seeing the answer)."""
        self._faults.setdefault(path, []).extend([fault] * times)

    @property
    def credits_spent(self) -> float:
        return round(15_000.0 - self.balance, 6)

    # ----- dispatch -----

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fault = self._faults.get(path, [None]).pop(0) if self._faults.get(path) else None
        if fault == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        if fault in ("429", "500", "403"):
            status = int(fault)
            error = {"429": "rate_limit_error", "500": "internal_error", "403": "permission_error"}[fault]
            headers = {"x-credits-used": "0", "x-ratelimit-reset": "1"}
            return httpx.Response(status, json={"error": {"type": error, "message": f"injected {fault}",
                                                          "metadata": []}}, headers=headers)

        body = json.loads(request.content) if request.content else {}
        routes = {
            ("POST", "/person/search"): self._person_search,
            ("POST", "/company/search"): self._company_search,
            ("POST", "/company/identify"): self._company_identify,
            ("POST", "/company/enrich"): self._company_enrich,
            ("POST", "/person/enrich"): self._person_enrich,
            ("POST", "/web/search/live"): self._web_search,
            ("POST", "/web/enrich/live"): self._web_fetch,
            ("GET", "/account/credits"): lambda _: ({"account": {"credits": round(self.balance, 6)}}, 0.0),
            ("GET", "/account/usage/events"): lambda _: ({"events": list(self.usage), "has_more": False,
                                                          "next_cursor": None}, 0.0),
            ("GET", "/account/endpoints"): lambda _: ({"api_version": "2025-11-01", "endpoints": []}, 0.0),
        }
        handler = routes.get((request.method, path))
        if handler is None:
            return httpx.Response(404, json={"error": {"type": "invalid_request", "message": "not found"}})
        result, credits = handler(body)
        request_id = str(uuid.uuid4())
        self.balance -= credits
        if credits:
            self.usage.append({"event_id": request_id, "ts": time.time(), "endpoint": path,
                               "http_status": 200, "credits_used": round(credits, 6)})
        if fault == "crash_after_charge":
            raise SimulatedCrash(f"process killed during {path}")
        return httpx.Response(200, json=result, headers={"x-credits-used": f"{credits:g}",
                                                         "x-request-id": request_id})

    # ----- endpoints -----

    def _person_search(self, body: dict):
        hits = [p for p in self.people if _matches(p, body.get("filters"))]
        page, cursor = _page(hits, body)
        return {"profiles": page, "next_cursor": cursor, "total_count": len(hits)}, \
            len(page) * PRICES["person_search_per_result"]

    def _company_search(self, body: dict):
        hits = [c for c in self.companies if _matches(c, body.get("filters"))]
        page, cursor = _page(hits, body)
        return {"companies": page, "next_cursor": cursor, "total_count": len(hits)}, \
            len(page) * PRICES["company_search_per_result"]

    def _company_identify(self, body: dict):
        match_types = {"domains": "domain", "names": "name", "crustdata_company_ids": "crustdata_company_id"}
        results, matched = [], 0
        for kind, values in body.items():
            if kind not in match_types:
                continue
            for value in values:
                found = [c for c in self.companies if _identifies(c, kind, value)]
                matched += bool(found)
                results.append({"matched_on": value, "match_type": match_types[kind],
                                "matches": [{"confidence_score": 1, "company_data": {
                                    "crustdata_company_id": c["crustdata_company_id"],
                                    "basic_info": {**c["basic_info"],
                                                   "crustdata_company_id": c["crustdata_company_id"]}}}
                                    for c in found]})
        return results, matched * PRICES["identify_per_match"]

    def _company_enrich(self, body: dict):
        results = []
        for cid in body.get("crustdata_company_ids", []):
            company = next((c for c in self.companies if c["crustdata_company_id"] == cid), None)
            data = None
            if company:
                staff = [p for p in self.people if _employer_id(p) == cid]
                leaders = [_short(p) for p in staff if _looks_senior(p)]
                data = {**company, "people": {"founders": [], "cxos": leaders, "decision_makers": leaders},
                        "news": [{"article_url": pg["url"], "article_title": pg["title"],
                                  "article_publish_date": pg["published"]}
                                 for pg in self.pages if company["basic_info"]["name"].split()[0] in pg["title"]]}
            results.append({"matched_on": cid, "match_type": "crustdata_company_id",
                            "matches": [{"confidence_score": 1, "company_data": data}] if data else []})
        return results, sum(PRICES["company_enrich_per_record"] for r in results if r["matches"])

    def _person_enrich(self, body: dict):
        results = []
        for url in body.get("professional_network_profile_urls", []):
            person = next((p for p in self.people if _profile_url(p) == url.rstrip("/")), None)
            results.append({"matched_on": url, "match_type": "professional_network_profile_url",
                            "match_status": "matched" if person else "not_found",
                            "matches": [{"confidence_score": 1, "person_data": person}] if person else []})
        return results, sum(PRICES["person_enrich_per_record"] for r in results if r["matches"])

    def _web_search(self, body: dict):
        words = [w.lower() for w in body.get("query", "").split() if len(w) > 2]
        hits = [pg for pg in self.pages
                if sum(w in (pg["title"] + " " + pg["snippet"]).lower() for w in words) >= max(1, len(words) // 2)]
        results = [{"source": "news", "title": pg["title"], "url": pg["url"], "snippet": pg["snippet"],
                    "position": i + 1} for i, pg in enumerate(hits)]
        return {"success": True, "query": body.get("query"), "timestamp": int(time.time() * 1000),
                "results": results, "metadata": {"total_results": len(results)}}, PRICES["web_search_per_page"]

    def _web_fetch(self, body: dict):
        results = []
        for url in body.get("urls", []):
            page = next((pg for pg in self.pages if pg["url"] == url), None)
            results.append({"success": bool(page), "url": url, "timestamp": int(time.time()),
                            "title": page["title"] if page else None, "content": page["content"] if page else None})
        return results, sum(PRICES["web_fetch_per_url"] for r in results if r["success"])


# ----- filter evaluation (the same filter language as the real API) -----------

def _matches(record: dict, flt: dict | None) -> bool:
    if not flt:
        return True
    if "op" in flt:
        results = [_matches(record, c) for c in flt["conditions"]]
        return any(results) if flt["op"] == "or" else all(results)
    path = FIELD_ALIASES.get(flt["field"], flt["field"])
    return any(_compare(v, flt["type"], flt.get("value")) for v in _values_at(record, path.split(".")))


def _values_at(node: Any, keys: list[str]) -> list[Any]:
    """All values at a dotted path, walking into lists along the way."""
    if not keys:
        return node if isinstance(node, list) else [node]
    if isinstance(node, list):
        return [v for item in node for v in _values_at(item, keys)]
    if isinstance(node, dict) and keys[0] in node:
        return _values_at(node[keys[0]], keys[1:])
    return [None]


def _compare(actual: Any, op: str, expected: Any) -> bool:
    def norm(x):
        return x.lower() if isinstance(x, str) else x
    if op == "is_null":
        return actual is None
    if op == "is_not_null":
        return actual is not None
    if actual is None:
        return False
    if op == "=":
        return norm(actual) == norm(expected)
    if op == "!=":
        return norm(actual) != norm(expected)
    if op == "in":
        return norm(actual) in [norm(e) for e in expected]
    if op == "not_in":
        return norm(actual) not in [norm(e) for e in expected]
    if op in ("(.)", "contains"):
        return all(word in str(actual).lower() for word in str(expected).lower().split())
    if op == "=>":
        return actual >= expected
    if op == "=<":
        return actual <= expected
    if op == ">":
        return actual > expected
    if op == "<":
        return actual < expected
    raise ValueError(f"fake Crustdata does not support filter type {op!r}")


def _page(hits: list, body: dict) -> tuple[list, str | None]:
    limit = int(body.get("limit", 20))
    start = int(body.get("cursor") or 0)
    page = hits[start:start + limit]
    return page, (str(start + limit) if start + limit < len(hits) else None)


def _identifies(company: dict, kind: str, value: Any) -> bool:
    info = company["basic_info"]
    if kind == "domains":
        return str(value).lower().removeprefix("www.") in info["all_domains"]
    if kind == "names":
        return str(value).lower() == info["name"].lower()
    return company["crustdata_company_id"] == value


def _employer_id(person: dict) -> int:
    return person["experience"]["employment_details"]["current"][0]["crustdata_company_id"]


def _profile_url(person: dict) -> str:
    return person["social_handles"]["professional_network_identifier"]["profile_url"]


def _looks_senior(person: dict) -> bool:
    title = person["basic_profile"]["current_title"].lower()
    return any(word in title for word in ("head", "vp", "director", "cto", "chief"))


def _short(person: dict) -> dict:
    return {"crustdata_person_id": person["crustdata_person_id"],
            "basic_profile": {"name": person["basic_profile"]["name"],
                              "current_title": person["basic_profile"]["current_title"]}}
