"""Tally Wallet over MCP: an agent's calls are capped, ledgered and refused with the numbers."""

from tally import wallet_mcp


async def test_an_agent_is_capped_and_told_why(settings):
    wallet_mcp.start(settings, cap=1.0)
    first = await wallet_mcp.crustdata_call("/company/identify", {"domains": ["paysetu.in"]})
    assert first["credits"] == 0.2 and first["response"][0]["matches"]

    again = await wallet_mcp.crustdata_call("/company/identify", {"domains": ["paysetu.in"]})
    assert again["from_ledger"] and again["credits"] == 0  # the same call is never bought twice

    too_big = await wallet_mcp.crustdata_call("/person/search", {"filters": {}, "limit": 50})
    assert "refused" in too_big and too_big["spent"] == 0.2

    unknown = await wallet_mcp.crustdata_call("/some/new/endpoint", {})
    assert "can't be priced" in unknown["refused"]

    status = await wallet_mcp.wallet_status()
    assert status["spent"] == 0.2 and status["left"] == 0.8 and len(status["calls"]) == 2
