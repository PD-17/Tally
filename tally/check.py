"""`tally check`: the first thing to run once the Crustdata key works (about 0.25 credits).

It makes the free account calls, one Company Identify (0.2) and a one-result
Person Search (about 0.03 + premium), then reads each answer the way the rest
of Tally will. If a field name differs from the docs, it shows up here, and
the fix belongs in tally/crust/schema.py.
"""

from __future__ import annotations

import json

from tally.config import Settings
from tally.crust import schema
from tally.crust.client import CrustClient


async def check(settings: Settings, domain: str = "crustdata.com") -> int:
    client = CrustClient(settings)
    problems = 0
    try:
        credits = await client.get("/account/credits")
        print(f"balance: {credits.body.get('account', {}).get('credits')} credits")

        endpoints = await client.get("/account/endpoints")
        for e in endpoints.body.get("endpoints", []):
            if e.get("path") in ("/person/search", "/company/search", "/company/identify", "/company/enrich",
                                 "/person/enrich", "/web/search/live", "/web/enrich/live"):
                print(f"  {e['path']:<20} {e.get('status')}, {e.get('effective_rate_limit_rpm')} a minute, "
                      f"base {e.get('base_credits')} credits")

        identify = await client.post("/company/identify", {"domains": [domain]})
        print(f"\nidentify {domain}: cost {identify.credits}, request {identify.request_id}")
        resolved = schema.identified(identify.body)
        print(f"  read as: {resolved}")
        if not resolved or resolved[0][1] is None:
            problems += 1
            print("  PROBLEM: no company read from the answer. Raw:", json.dumps(identify.body)[:800])
            return problems
        company = resolved[0][1]

        search = await client.post("/person/search", {
            "filters": {"field": "experience.employment_details.current.company_id", "type": "in",
                        "value": [company.company_id]},
            "limit": 1,
        })
        print(f"\nperson search at {company.name}: cost {search.credits}, request {search.request_id}")
        profiles = search.body.get("profiles", [])
        if not profiles:
            print("  no profiles returned (try another domain).")
        else:
            person = schema.candidate_from_profile(profiles[0])
            print(f"  read as: {person}")
            missing = [name for name in ("name", "title", "company_id", "updated_at") if not getattr(person, name)]
            if missing:
                problems += 1
                print(f"  PROBLEM: couldn't read {missing}. Raw profile:", json.dumps(profiles[0])[:1500])
        print("\nall readable" if not problems else f"\n{problems} problem(s): fix tally/crust/schema.py")
        return problems
    finally:
        await client.aclose()
