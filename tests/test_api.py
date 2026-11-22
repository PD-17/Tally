"""The web flow: deck -> quote -> accept -> live checks -> memo -> question approval -> audit."""

import asyncio

import httpx

from tally.api import create_app

async def wait_for(client, path, tries=100):
    for _ in range(tries):
        response = await client.get(path)
        if response.status_code == 200:
            return response.json()
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} never became ready")


async def test_the_memo_flow_end_to_end(settings):
    import base64
    from pathlib import Path

    deck = (Path(__file__).parent / "decks" / "kredbloom_altered.txt").read_bytes()
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://desk") as client:
        body = {"deck_b64": base64.b64encode(deck).decode(), "filename": "kredbloom.txt",
                "founders": ["Leela Das", "Arjun Nair"], "budget": 150, "minutes": 8}
        run_id = (await client.post("/api/memos", json=body)).json()["run_id"]
        desk = app.state.desk
        for _ in range(100):
            if desk.runs[run_id].quote_card:
                break
            await asyncio.sleep(0.05)
        assert desk.runs[run_id].quote_card["claims"] == 9
        await client.post(f"/api/jobs/{run_id}/accept")
        memo = await wait_for(client, f"/api/memos/{run_id}")
        assert sum(c["verdict"] == "contradicted" for c in memo["claims"]) == 3
        assert memo["audit"]["matches"] and memo["bill"]["ledger_chain_ok"]
        wrong = await client.post(f"/api/memos/{run_id}/questions", json={"binding": "x", "approve": True})
        assert wrong.status_code == 409
        ok = await client.post(f"/api/memos/{run_id}/questions",
                               json={"binding": memo["questions_binding"], "approve": True})
        assert ok.json()["outbox"]
        assert "Diligence Desk" in (await client.get("/")).text
