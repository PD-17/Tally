"""Tally Wallet as an MCP server: any agent can use Crustdata, under a cap it can't get around.

    claude mcp add tally-wallet -- uv --directory <this repo> run tally wallet --cap 20

The agent gets one tool to call Crustdata and one to see what it has spent.
Every call is priced at its worst case before it is made, goes through the
same wallet and ledger as Tally's own runs, and is refused, politely and with
the numbers, once it would pass the cap. An endpoint the wallet can't price
is refused too: a call that can't be priced can't be capped.
"""

from __future__ import annotations

import time
from datetime import date

from mcp.server.fastmcp import FastMCP

from tally.config import Settings, load_settings
from tally.crust.client import CrustClient
from tally.crust.fake import FakeCrustdata
from tally.ledger import Ledger, call_key
from tally.prices import PriceBook
from tally.wallet import BudgetNode, BudgetRefused, Wallet

server = FastMCP("tally-wallet")
_session: dict = {}  # created by `start()`: settings, ledger, client, wallet


def worst_case(endpoint: str, body: dict, prices: PriceBook) -> float | None:
    """The most a call can cost, from its request alone; None if we can't tell."""
    identifiers = sum(len(v) for k, v in body.items() if isinstance(v, list) and k != "fields")
    if endpoint == "/person/search":
        return prices.person_search(int(body.get("limit") or body.get("count") or 20))
    if endpoint == "/company/search":
        return prices.company_search(int(body.get("limit") or 20), ["taxonomy", "headcount", "funding", "hiring"])
    if endpoint == "/company/identify":
        return prices.identify(identifiers)
    if endpoint == "/company/enrich":
        return identifiers * prices.company_enrich_record * 2  # technographics can double it
    if endpoint == "/person/enrich":
        return identifiers * 9.0  # the documented maximum per record, with every contact field
    if endpoint == "/web/search/live":
        return prices.web_search_page * int(body.get("page") or 1)  # page N costs N
    if endpoint == "/web/enrich/live":
        return prices.web_fetch_url * len(body.get("urls", []))
    return None


def start(settings: Settings, cap: float) -> None:
    ledger = Ledger(settings.data_dir / "ledger.sqlite")
    run_id = f"wallet-{date.today().isoformat()}-{int(time.time())}"
    ledger.start_run(run_id, "Tally Wallet (MCP)", cap, time.time() + 24 * 3600, settings.mode, client_id="mcp")
    transport = FakeCrustdata().transport() if settings.mode == "fake" else None
    wallet = Wallet(BudgetNode("mcp-caller", cap), ledger, run_id, deadline=time.time() + 24 * 3600,
                    approve_over=float("inf"))  # the cap itself is the control here
    _session.update(settings=settings, ledger=ledger, client=CrustClient(settings, transport=transport),
                    wallet=wallet, prices=PriceBook())


@server.tool()
async def crustdata_call(endpoint: str, body: dict) -> dict:
    """Call a Crustdata API endpoint (POST, e.g. "/person/search") with a JSON body, through a capped
    wallet. Returns the response with its cost, or a refusal saying what was spent and what is left."""
    wallet: Wallet = _session["wallet"]
    price = worst_case(endpoint, body, _session["prices"])
    status = {"spent": round(wallet.spent(), 4), "cap": wallet.root.limit}
    if price is None:
        return {"refused": f"{endpoint} can't be priced in advance, so the wallet won't buy it", **status}

    async def perform():
        response = await _session["client"].post(endpoint, body)
        return response.body, response.credits, response.request_id

    try:
        bought = await wallet.buy(wallet.root, call_key(wallet.run_id, endpoint, body), endpoint, price,
                                  "called by an agent over MCP", perform)
    except BudgetRefused as refused:
        return {"refused": f"this call could cost up to {price:g} credits and only "
                           f"{max(refused.available, 0):g} are left under the cap", **status}
    return {"credits": bought.credits, "request_id": bought.request_id, "from_ledger": bought.from_ledger,
            "spent": round(wallet.spent(), 4), "cap": wallet.root.limit, "response": bought.body}


@server.tool()
async def wallet_status() -> dict:
    """What this agent has spent, its cap, and every call on the ledger."""
    wallet: Wallet = _session["wallet"]
    calls = [{"endpoint": s.endpoint, "credits": s.charged, "request_id": s.request_id, "state": s.event}
             for s in _session["ledger"].states(wallet.run_id) if s.kind == "crustdata"]
    return {"spent": round(wallet.spent(), 4), "cap": wallet.root.limit,
            "left": round(wallet.root.available(), 4), "calls": calls}


def serve(cap: float, settings: Settings | None = None) -> None:
    """Run the MCP server over stdio."""
    start(settings or load_settings(), cap)
    server.run()
