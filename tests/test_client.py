import pytest

from tally.config import load_settings
from tally.crust.client import CrustClient
from tally.crust.errors import EndpointForbidden, SourceTimeout
from tests.conftest import no_wait

COMPANY_FILTER = {"field": "experience.employment_details.current.company_id", "type": "in", "value": [1001]}


async def test_cost_and_request_id_come_back_with_every_call(client, fake):
    response = await client.post("/person/search", {"filters": COMPANY_FILTER, "limit": 10})
    assert [p["basic_profile"]["name"] for p in response.body["profiles"]] == ["Ananya Rao", "Rahul Sharma"]
    assert response.credits == pytest.approx(0.06)
    assert response.request_id
    assert fake.credits_spent == pytest.approx(0.06)


async def test_429_and_500_are_retried_and_not_charged(client, fake):
    fake.inject("/company/identify", "429")
    fake.inject("/company/identify", "500")
    response = await client.post("/company/identify", {"domains": ["ledgerline.com"]})
    assert response.body[0]["matches"][0]["company_data"]["crustdata_company_id"] == 1002
    assert response.credits == pytest.approx(0.2)


async def test_403_and_timeouts_are_raised_for_the_caller(client, fake):
    fake.inject("/web/search/live", "403")
    with pytest.raises(EndpointForbidden):
        await client.post("/web/search/live", {"query": "Upitra engineering"})
    fake.inject("/web/search/live", "timeout")
    with pytest.raises(SourceTimeout):
        await client.post("/web/search/live", {"query": "Upitra engineering"})


async def test_record_then_replay_serves_the_same_answer_for_free(tmp_path, fake):
    recording = load_settings(mode="record", data_dir=tmp_path, crustdata_api_key="test-key")
    recorder = CrustClient(recording, transport=fake.transport(), sleep=no_wait)
    live = await recorder.post("/company/identify", {"domains": ["paysetu.in"]})
    await recorder.aclose()

    replaying = load_settings(mode="replay", data_dir=tmp_path, crustdata_api_key=None)
    player = CrustClient(replaying, sleep=no_wait)
    replayed = await player.post("/company/identify", {"domains": ["paysetu.in"]})
    await player.aclose()

    assert replayed.body == live.body
    assert replayed.credits == 0.0 and replayed.request_id.startswith("replay:")


def test_live_mode_needs_a_key(tmp_path):
    with pytest.raises(Exception, match="CRUSTDATA_API_KEY"):
        CrustClient(load_settings(mode="live", data_dir=tmp_path, crustdata_api_key=None))
