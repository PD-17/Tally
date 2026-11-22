"""The web flow: job -> quote -> accept -> live run with a cut from the phone -> report -> audit."""

import asyncio

import httpx

from tally.api import create_app

JOB = {"job": "Find the head of engineering at 10 Indian fintech companies with 50-500 people",
       "budget": 400, "minutes": 20, "race": True}


async def wait_for(client, path, tries=100):
    for _ in range(tries):
        response = await client.get(path)
        if response.status_code == 200:
            return response.json()
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} never became ready")


async def test_the_desk_flow_end_to_end(settings):
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://desk") as client:
        run_id = (await client.post("/api/jobs", json=JOB)).json()["run_id"]
        desk = app.state.desk
        for _ in range(100):  # the quote is worked out in the background
            if desk.runs[run_id].quote_card:
                break
            await asyncio.sleep(0.05)
        assert desk.runs[run_id].quote_card.companies == 8

        assert (await client.post(f"/api/jobs/{run_id}/accept")).json() == {"ok": True}
        cut = (await client.post(f"/api/jobs/{run_id}/cut", json={"factor": 0.5})).json()
        assert cut["limit"] < 400

        report = await wait_for(client, f"/api/jobs/{run_id}/report")
        assert report["bill"]["companies"] == 8 and report["bill"]["ledger_chain_ok"]
        assert all(row["calls"] for row in report["rows"])  # every row has a receipt

        audit = (await client.get(f"/api/jobs/{run_id}/audit")).json()
        assert audit["chain_ok"]
        page = await client.get("/")
        assert "Tally" in page.text
        info = (await client.get("/api/info")).json()
        assert info["quote_share"] == 0.2  # the desk shows "spends up to" from this before the click
