"""The purchases Tally can make, each one through the wallet.

A probe builds the request, works out its worst-case price, buys it and
parses the answer. Requests are deterministic, so running the same run again
(after a crash, say) finds every past purchase in the ledger and pays nothing
for it: the run continues where it stopped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from tally.crust import schema
from tally.crust.client import CrustClient
from tally.crust.errors import SourceTimeout
from tally.ledger import Ledger, call_key
from tally.models import Candidate, Company, JobSpec
from tally.prices import PriceBook
from tally.wallet import BudgetNode, Wallet

T = TypeVar("T")

MAX_ATTEMPTS = 3  # a timed-out call is retried twice, each as a new purchase

PERSON_FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "basic_profile.current_title",
    "experience.employment_details.current",
    "social_handles.professional_network_identifier.profile_url",
    "metadata.updated_at",
]
STOP_WORDS = {"of", "the", "and", "&", "-", "–", "head-", "at"}


@dataclass(frozen=True)
class Bought(Generic[T]):
    """A parsed answer and the purchase behind it, for the receipt."""

    value: T
    call_key: str
    credits: float
    request_id: str | None


class Buyer:
    def __init__(self, wallet: Wallet, client: CrustClient, prices: PriceBook, ledger: Ledger):
        self.wallet = wallet
        self.client = client
        self.prices = prices
        self.ledger = ledger
        self.run_id = wallet.run_id

    async def find_companies(self, node: BudgetNode, spec: JobSpec,
                             limit: int) -> Bought[tuple[list[Company], int | None]]:
        """Up to `limit` companies matching the client's filters (0.03 a result plus premium
        filters), and how many match in all."""
        conditions = []
        if spec.country:
            conditions.append({"field": "locations.country", "type": "=", "value": spec.country})
        if spec.industry:
            conditions.append({"field": "taxonomy.professional_network_industry", "type": "=", "value": spec.industry})
        if spec.min_headcount is not None:
            conditions.append({"field": "headcount.total", "type": "=>", "value": spec.min_headcount})
        if spec.max_headcount is not None:
            conditions.append({"field": "headcount.total", "type": "=<", "value": spec.max_headcount})
        body = {"filters": {"op": "and", "conditions": conditions}, "limit": limit}
        worst_case = self.prices.company_search(limit, self.company_search_premium(spec))
        bought = await self._buy(node, "/company/search", body, worst_case, f"find {limit} companies matching the job")
        companies = [schema.company_from_record(r) for r in bought.value.get("companies", [])]
        return Bought((companies, bought.value.get("total_count")), bought.call_key, bought.credits,
                      bought.request_id)

    def company_search_premium(self, spec: JobSpec) -> list[str]:
        """The premium filter groups `find_companies` will use for this job."""
        groups = ["taxonomy"] if spec.industry else []
        if spec.min_headcount is not None or spec.max_headcount is not None:
            groups.append("headcount")
        return groups

    async def identify(self, node: BudgetNode, domains: list[str]) -> Bought[list[tuple[str, Company | None]]]:
        """Resolve the client's own list to one record per company (0.2 per match)."""
        body = {"domains": sorted({d.lower().removeprefix("www.") for d in domains})}
        bought = await self._buy(node, "/company/identify", body, self.prices.identify(len(body["domains"])),
                                 f"resolve {len(body['domains'])} domains to companies")
        return Bought(schema.identified(bought.value), bought.call_key, bought.credits, bought.request_id)

    async def people_at(self, node: BudgetNode, companies: list[Company], titles: list[str],
                        per_company: int = 4) -> Bought[dict[int, list[Candidate]]]:
        """One person search across many companies: who holds a matching title at each."""
        ids = sorted(c.company_id for c in companies)
        title_filters = [{"field": "experience.employment_details.current.title", "type": "(.)", "value": term}
                         for term in search_terms(titles)]
        body = {
            "filters": {"op": "and", "conditions": [
                {"field": "experience.employment_details.current.company_id", "type": "in", "value": ids},
                {"op": "or", "conditions": title_filters},
            ]},
            "limit": min(len(ids) * per_company, 1000),
            "fields": PERSON_FIELDS,
        }
        bought = await self._buy(node, "/person/search", body, self.prices.person_search(body["limit"]),
                                 f"search engineering leaders at {len(ids)} companies")
        by_company: dict[int, list[Candidate]] = {i: [] for i in ids}
        for profile in bought.value.get("profiles", []):
            person = schema.candidate_from_profile(profile)
            if person.company_id in by_company:
                by_company[person.company_id].append(person)
        return Bought(by_company, bought.call_key, bought.credits, bought.request_id)

    async def leaders_of(self, node: BudgetNode, company: Company) -> Bought[list[Candidate]]:
        """Company Enrich's people section: the CXOs and decision makers (2 credits)."""
        body = {"crustdata_company_ids": [company.company_id], "fields": ["people"]}
        bought = await self._buy(node, "/company/enrich", body, self.prices.company_enrich(1),
                                 f"ask {company.name}'s record for its leadership")
        leaders = schema.enriched_leaders(bought.value).get(company.company_id, [])
        return Bought(leaders, bought.call_key, bought.credits, bought.request_id)

    async def news_about(self, node: BudgetNode, company: Company, person: Candidate) -> Bought[list[dict]]:
        """Recent web and news results about this person at this company (1 credit)."""
        body = {"query": f'"{person.name}" {company.name} engineering', "sources": ["news", "web"]}
        bought = await self._buy(node, "/web/search/live", body, self.prices.web_search(),
                                 f"check news on {person.name} at {company.name}")
        return Bought(schema.web_results(bought.value), bought.call_key, bought.credits, bought.request_id)

    async def confirm(self, node: BudgetNode, person: Candidate) -> Bought[Candidate | None]:
        """The person's full current record, by profile URL (1 credit, no contact fields)."""
        body = {"professional_network_profile_urls": [person.profile_url],
                "fields": ["basic_profile", "experience"]}
        bought = await self._buy(node, "/person/enrich", body, self.prices.person_enrich(1),
                                 f"confirm {person.name}'s current role")
        return Bought(schema.enriched_person(bought.value), bought.call_key, bought.credits, bought.request_id)

    # ----- the one place a purchase happens -----

    async def _buy(self, node: BudgetNode, endpoint: str, body: dict, est_max: float, reason: str) -> Bought[Any]:
        """Buy through the wallet. A call left in doubt by a timeout or crash is re-bought
        under a new key, so the in-doubt one stays on the ledger for reconciliation."""
        for attempt in range(MAX_ATTEMPTS):
            key = call_key(self.run_id, endpoint, body, attempt)
            spent_without_answer = self.ledger.state(key) is not None and self.ledger.saved_response(key) is None
            if spent_without_answer:
                continue  # in doubt, failed, or charged with the answer lost: use a fresh attempt

            async def perform():
                response = await self.client.post(endpoint, body)
                return response.body, response.credits, response.request_id

            try:
                purchase = await self.wallet.buy(node, key, endpoint, est_max, reason, perform)
            except SourceTimeout:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                continue
            return Bought(purchase.body, key, purchase.credits, purchase.request_id)
        raise SourceTimeout(f"{endpoint}: gave up after {MAX_ATTEMPTS} attempts")


def search_terms(titles: list[str]) -> list[str]:
    """Title phrases as all-words searches: 'Head of Engineering' -> 'head engineering'."""
    terms = []
    for title in titles:
        words = [w for w in title.lower().replace(",", " ").split() if w not in STOP_WORDS]
        term = " ".join(words)
        if term and term not in terms:
            terms.append(term)
    return terms
