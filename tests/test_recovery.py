"""Pull the plug: a crash mid-call is neither counted twice nor missed."""

from datetime import date
from pathlib import Path

import pytest

from tally.crust.fake import FakeCrustdata, SimulatedCrash
from tally.diligence.deck import load_deck
from tally.diligence.run import DiligenceRun
from tally.ledger import Ledger

DECK = Path(__file__).parent / "decks" / "kredbloom_altered.txt"
FOUNDERS = ["Leela Das", "Arjun Nair"]
TODAY = date(2026, 11, 22)


def make_run(settings, fake, ledger, run_id):
    return DiligenceRun(settings, load_deck(DECK), 150.0, 8, founders=FOUNDERS, run_id=run_id,
                        transport=fake.transport(), today=TODAY, ledger=ledger)


def verdicts(memo):
    return {(r.claim.kind.value, r.claim.subject, r.claim.number): r.verdict for r in memo.results}


@pytest.mark.parametrize("endpoint", ["/company/enrich", "/person/enrich", "/web/search/live", "/job/search"])
async def test_a_crash_after_crustdata_charged_is_recovered_exactly(settings, fake, ledger, tmp_path, endpoint):
    fake.inject(endpoint, "crash_after_charge")
    with pytest.raises(SimulatedCrash):
        await make_run(settings, fake, ledger, "crash-test").execute()
    assert ledger.open_reservations(), "the dead process should leave its purchase open"

    memo = await make_run(settings, fake, ledger, "crash-test").execute()

    assert ledger.open_reservations() == []
    assert memo.credits == pytest.approx(fake.credits_spent)  # every charge counted, none twice
    assert memo.balance.matches  # the ledger equals the drop in Crustdata's balance
    assert ledger.verify_chain().ok

    # The verdicts are the same as a run that never crashed.
    calm_ledger = Ledger(tmp_path / "calm.sqlite")
    calm = await make_run(settings, FakeCrustdata(), calm_ledger, "calm").execute()
    assert verdicts(memo) == verdicts(calm)
    calm_ledger.close()


async def test_a_restart_after_a_finished_memo_buys_nothing_again(settings, fake, ledger):
    first = await make_run(settings, fake, ledger, "twice").execute()
    spent = fake.credits_spent
    again = await make_run(settings, fake, ledger, "twice").execute()
    assert fake.credits_spent == spent  # everything came back from the ledger
    assert again.credits == pytest.approx(first.credits)
