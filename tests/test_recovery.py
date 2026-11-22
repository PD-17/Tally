"""Pull the plug: a crash mid-call is neither counted twice nor missed."""

import pytest

from tally.crust.fake import FakeCrustdata, SimulatedCrash
from tally.ledger import Ledger
from tally.runner import Run
from tests.test_run import JOB, TODAY


def make_run(settings, fake, ledger, run_id):
    return Run(settings, JOB, 400.0, minutes=20, run_id=run_id, transport=fake.transport(), today=TODAY,
               ledger=ledger)


@pytest.mark.parametrize("endpoint", ["/person/search", "/company/enrich", "/web/search/live"])
async def test_a_crash_after_crustdata_charged_is_recovered_exactly(settings, fake, ledger, tmp_path, endpoint):
    fake.inject(endpoint, "crash_after_charge")
    with pytest.raises(SimulatedCrash):
        await make_run(settings, fake, ledger, "crash-test").execute()
    assert ledger.open_reservations(), "the dead process should leave its purchase open"

    result = await make_run(settings, fake, ledger, "crash-test").execute()

    assert ledger.open_reservations() == []
    assert result.credits == pytest.approx(fake.credits_spent)  # every charge counted, none twice
    assert result.balance.matches  # the ledger equals the drop in Crustdata's balance
    assert ledger.verify_chain().ok

    # The answers are the same as a run that never crashed.
    calm_ledger = Ledger(tmp_path / "calm.sqlite")
    calm = await make_run(settings, FakeCrustdata(), calm_ledger, "calm").execute()
    assert {a.company.name: a.status for a in result.answers} == {a.company.name: a.status for a in calm.answers}
    calm_ledger.close()


async def test_a_restart_after_a_finished_run_buys_nothing_again(settings, fake, ledger):
    first = await make_run(settings, fake, ledger, "twice").execute()
    spent = fake.credits_spent
    again = await make_run(settings, fake, ledger, "twice").execute()
    assert fake.credits_spent == spent  # everything came back from the ledger
    assert again.credits == pytest.approx(first.credits)
