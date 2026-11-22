"""`tally check`: the first thing to run once the Crustdata key works.

Basic (about 2.2 credits): the free account calls, one Company Identify (0.2) and
one Company Enrich (2) sent exactly as a deck check sends it. `--full` adds a Job
Search, a comparables Company Search and a Web Search (about 5 credits more).

It reads every answer the way the rest of Tally will, saves each raw response
to <data dir>/check/ so a field-name mismatch can be fixed in
tally/crust/schema.py, and then checks the two things the ledger depends on:
that the usage records list our request ids, and that the balance dropped by
exactly what X-Credits-Used said.
"""

from __future__ import annotations

import json
from pathlib import Path

from tally.config import Settings
from tally.crust import schema
from tally.crust.client import CrustClient
from tally.crust.fake import FakeCrustdata
from tally.diligence.probes import COMPARABLE_FIELDS, JOB_FIELDS, PROFILE_FIELDS

PAID_PATHS = ("/person/search", "/company/search", "/company/identify", "/company/enrich",
              "/person/enrich", "/web/search/live", "/web/enrich/live")


class Checker:
    def __init__(self, settings: Settings):
        # --mode fake runs the whole check offline against the made-up world (a dry run).
        transport = FakeCrustdata().transport() if settings.mode == "fake" else None
        self.client = CrustClient(settings, transport=transport)
        self.out = Path(settings.data_dir) / "check"
        self.out.mkdir(parents=True, exist_ok=True)
        self.problems: list[str] = []
        self.charges: list[tuple[str, float, str | None]] = []  # (endpoint, credits, request id)

    def save(self, name: str, body) -> None:
        (self.out / f"{name}.json").write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")

    def problem(self, text: str) -> None:
        self.problems.append(text)
        print(f"  PROBLEM: {text}")

    async def get(self, name: str, path: str, params: dict | None = None):
        response = await self.client.get(path, params)
        self.save(name, response.body)
        return response

    async def post(self, name: str, path: str, body: dict):
        response = await self.client.post(path, body)
        self.save(name, response.body)
        self.charges.append((path, response.credits, response.request_id))
        print(f"\n{path}: cost {response.credits:g}, request id {response.request_id}")
        if response.request_id is None:
            self.problem(f"{path} sent no X-Request-Id: reconciliation can't match it")
        return response


async def check(settings: Settings, domain: str = "razorpay.com", full: bool = False) -> int:
    c = Checker(settings)
    if settings.mode == "fake" and domain == "razorpay.com":
        domain = "ledgerline.in"  # a company in the made-up world
    try:
        start = await c.get("account_credits", "/account/credits")
        start_balance = _balance(start.body)
        print(f"balance: {start_balance} credits")
        if start_balance is None:
            c.problem("couldn't read body['account']['credits'] (see check/account_credits.json); "
                      "fix tally/recovery.py account_balance()")

        endpoints = await c.get("account_endpoints", "/account/endpoints")
        listed = {e.get("path"): e for e in endpoints.body.get("endpoints", [])}
        print("\nendpoints (status, rate limit, base credits):")
        for path in PAID_PATHS:
            e = listed.get(path)
            print(f"  {path:<20} " + (f"{e.get('status')}, {e.get('effective_rate_limit_rpm')}/min, "
                                      f"base {e.get('base_credits')}" if e else "NOT LISTED"))

        usage = await c.get("usage_before", "/account/usage/events", {"limit": 5})
        events = usage.body.get("events")
        if events is None:
            c.problem("usage records have no 'events' list (see check/usage_before.json); fix tally/recovery.py")
        elif events:
            missing = [k for k in ("event_id", "endpoint", "credits_used") if k not in events[0]]
            print(f"\nusage records: {len(events)} recent, fields {sorted(events[0])}")
            if missing:
                c.problem(f"usage events lack {missing}; tally/recovery.py reads those names")

        # ----- paid calls -----
        identify = await c.post("company_identify", "/company/identify", {"domains": [domain]})
        resolved = schema.identified(identify.body)
        print(f"  read as: {resolved}")
        if not resolved or resolved[0][1] is None:
            c.problem("no company read from Identify (see check/company_identify.json); fix schema.identified")
            return _summary(c)
        company = resolved[0][1]

        # The exact request DeckBuyer.company_profile sends.
        enrich = await c.post("company_enrich", "/company/enrich",
                              {"crustdata_company_ids": [company.company_id], "fields": PROFILE_FIELDS})
        profile = schema.company_profile(enrich.body)
        if profile is None:
            c.problem("no company read from Company Enrich (see check/company_enrich.json); fix schema.company_profile")
            return _summary(c)
        print(f"  read as: {profile.name}, {profile.headcount} people ({profile.headcount_date}), "
              f"funding {profile.funding_total_usd}, {len(profile.rounds)} rounds, "
              f"{len(profile.all_investors())} investors, {len(profile.founders)} founders "
              f"({sum(bool(f.past) for f in profile.founders)} with work history), "
              f"{len(profile.competitor_ids)} competitor ids, {len(profile.competitor_domains)} competitor domains")
        for name in ("headcount", "rounds", "founders"):
            if not getattr(profile, name):
                print(f"  note: no {name} on this record; try another --domain to be sure the field names are right")

        if full:
            await _full(c, profile)

        # ----- does the ledger's view match Crustdata's? -----
        after = await c.get("usage_after", "/account/usage/events", {"limit": 50})
        seen = {e.get("event_id") for e in after.body.get("events", [])}
        for path, credits, request_id in c.charges:
            if credits > 0 and request_id not in seen:
                print(f"  note: {path} ({request_id}) not in usage records yet; they may lag. "
                      f"If it never appears, recovery can't match by request id.")
        end_balance = _balance((await c.get("account_credits_after", "/account/credits")).body)
        charged = sum(credits for _, credits, _ in c.charges)
        if start_balance is not None and end_balance is not None:
            drop = round(start_balance - end_balance, 6)
            print(f"\nbalance drop {drop:g} vs X-Credits-Used total {charged:g}")
            if abs(drop - charged) >= 0.005:
                c.problem("balance drop != sum of X-Credits-Used: the end-of-run check will fail")
        return _summary(c)
    finally:
        await c.client.aclose()


async def _full(c: Checker, profile) -> None:
    jobs = await c.post("job_search", "/job/search", {
        "filters": {"field": "company.basic_info.crustdata_company_id", "type": "in", "value": [profile.company_id]},
        "limit": 5, "fields": JOB_FIELDS})
    posts = schema.job_posts(jobs.body)
    print(f"  read as: {posts.total} posts" + (f", newest {posts.posts[0].title} ({posts.posts[0].date})"
                                                if posts.posts else ""))
    domains = profile.competitor_domains[:5]
    if profile.competitor_ids or domains:
        flt = ({"field": "crustdata_company_id", "type": "in", "value": profile.competitor_ids[:5]}
               if profile.competitor_ids else {"field": "basic_info.primary_domain", "type": "in", "value": domains})
        comps = await c.post("company_search", "/company/search", {"filters": flt, "limit": 5,
                                                                   "fields": COMPARABLE_FIELDS})
        found = schema.comparables(comps.body)
        print(f"  read as: {[(x.name, x.headcount, x.funding_total_usd) for x in found]}")
        if not found:
            c.problem("no comparables read (see check/company_search.json); fix schema.comparables")
    web = await c.post("web_search", "/web/search/live",
                       {"query": f"{profile.name} raises funding", "sources": ["news", "web"]})
    results = schema.web_results(web.body)
    print(f"  read as: {len(results)} results" + (f", first: {results[0]}" if results else ""))
    if not results:
        c.problem("no web results read (see check/web_search.json); fix schema.web_results")


def _balance(body) -> float | None:
    try:
        return float(body["account"]["credits"])
    except (KeyError, TypeError, ValueError):
        return None


def _summary(c: Checker) -> int:
    print(f"\nraw responses saved in {c.out}")
    if not c.problems:
        print("all readable")
    else:
        print(f"{len(c.problems)} problem(s):")
        for p in c.problems:
            print(f"  - {p}")
    return len(c.problems)
