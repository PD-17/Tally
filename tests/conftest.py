"""Shared fixtures: a fresh ledger, offline settings and the made-up Crustdata."""

import pytest

from tally.config import load_settings
from tally.crust.client import CrustClient
from tally.crust.fake import FakeCrustdata
from tally.ledger import Ledger


async def no_wait(_seconds: float) -> None:
    """Stands in for asyncio.sleep so retry tests run instantly."""


@pytest.fixture
def settings(tmp_path):
    return load_settings(mode="fake", data_dir=tmp_path / "data", crustdata_api_key=None, inr_per_usd=88.0)


@pytest.fixture
def ledger(tmp_path):
    book = Ledger(tmp_path / "ledger.sqlite")
    yield book
    book.close()


@pytest.fixture
def fake():
    return FakeCrustdata()


@pytest.fixture
async def client(settings, fake):
    crust = CrustClient(settings, transport=fake.transport(), sleep=no_wait)
    yield crust
    await crust.aclose()
