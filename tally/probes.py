"""The one place a purchase happens: build the request, price its worst case, buy it through the
wallet. Requests are deterministic, so running the same run again (after a crash, say) finds every
past purchase in the ledger and pays nothing for it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from tally.crust.client import CrustClient
from tally.crust.errors import SourceTimeout
from tally.ledger import Ledger, call_key
from tally.prices import PriceBook
from tally.wallet import BudgetNode, Wallet

T = TypeVar("T")

MAX_ATTEMPTS = 3  # a timed-out call is retried twice, each as a new purchase


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
