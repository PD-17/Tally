"""One job end to end, against the made-up world in tally/crust/fake.py."""

from datetime import date

import pytest

from tally.intake import read_job_with_rules
from tally.models import Status
from tally.runner import Run, quote_cap
from tally.verifier import TitleVerdict, judge_title

TODAY = date(2026, 11, 22)
JOB = "Find the head of engineering at 10 Indian fintech companies with 50-500 people"


async def run_job(settings, fake, ledger, job=JOB, budget=400.0, **kwargs):
    run = Run(settings, job, budget, minutes=20, transport=fake.transport(), today=TODAY, ledger=ledger, **kwargs)
    return await run.execute()


def by_company(result):
    return {a.company.name: a for a in result.answers}


async def test_each_company_gets_its_honest_answer(settings, fake, ledger):
    result = await run_job(settings, fake, ledger)
    answers = by_company(result)

    assert "Finvolt Energy" not in answers  # the lookalike is a different industry
    assert "Bharat Megabank Tech" not in answers and "Lendloop" not in answers  # too big; not in India
    assert answers["Ledgerline"].status is Status.CONFIRMED  # news agrees with the record
    assert answers["Coinwell"].person.name == "Ishita Bose"  # the VP, not the engineering manager
    assert answers["Upitra"].status is Status.CONFLICT  # the record says yes, newer news says she left
    assert answers["Quietpay"].status is Status.CANT_VERIFY  # nobody on record, and it says so
    assert answers["Kredbloom"].person.title == "Co-founder & CTO"  # a small firm's CTO counts
    assert answers["Taxmitra"].status is Status.PROBABLE  # a borderline title is never "confirmed"


async def test_the_ledger_matches_what_crustdata_charged(settings, fake, ledger):
    result = await run_job(settings, fake, ledger)
    assert result.credits == pytest.approx(fake.credits_spent)
    assert ledger.verify_chain().ok
    every_row = sum(a.credits for a in result.answers)
    assert every_row <= result.credits + 1e-9  # receipts never claim more than was spent


async def test_one_company_under_two_domains_stays_one_company(settings, fake, ledger):
    job = "Head of engineering at ledgerline.in, ledgerline.com and finvolt.in"
    result = await run_job(settings, fake, ledger, job=job)
    assert sorted(by_company(result)) == ["Finvolt", "Ledgerline"]
    assert {"ledgerline.in", "ledgerline.com"} <= by_company(result)["Ledgerline"].company.aliases


async def test_a_tiny_budget_stops_early_and_still_answers_honestly(settings, fake, ledger):
    result = await run_job(settings, fake, ledger, budget=1.0)
    assert result.credits <= 1.0
    assert all(a.status in (Status.NOT_REACHED, Status.CANT_VERIFY, Status.PROBABLE, Status.CONFIRMED,
                            Status.CONFLICT) for a in result.answers)


async def test_a_job_outside_the_service_is_declined_clearly(settings, fake, ledger):
    result = await run_job(settings, fake, ledger, job="Sign our three-person team up for data tools under ₹50,000")
    assert result.answers == [] and result.stop_reason.startswith("unsupported")
    assert fake.credits_spent == 0


def test_the_rules_read_the_example_job():
    spec = read_job_with_rules(JOB)
    assert (spec.company_count, spec.country, spec.industry) == (10, "India", "Financial Services")
    assert (spec.min_headcount, spec.max_headcount) == (50, 500)


@pytest.mark.parametrize("title, headcount, verdict", [
    ("Head of Engineering", 300, TitleVerdict.STRONG),
    ("VP of Engineering", 300, TitleVerdict.STRONG),
    ("Co-founder & CTO", 60, TitleVerdict.STRONG),
    ("CTO", 2000, TitleVerdict.BORDERLINE),
    ("Engineering Manager", 300, TitleVerdict.BORDERLINE),
    ("Head - Technology", 75, TitleVerdict.BORDERLINE),
    ("Senior Software Engineer", 300, TitleVerdict.NO),
    ("Product Manager", 90, TitleVerdict.NO),
])
def test_title_rules(title, headcount, verdict):
    assert judge_title(title, headcount) is verdict


async def test_tally_beats_the_naive_buyer_on_the_same_job(settings, ledger):
    from functools import partial

    from tally.crust.fake import FakeCrustdata
    from tally.race import race, scoreboard

    world = FakeCrustdata()
    make_run = partial(Run, settings, transport=world.transport(), today=TODAY, ledger=ledger)
    tally, naive = await race(make_run, JOB, 400.0, 20, run_id="race")
    board = scoreboard(tally, naive)

    assert board["tally"]["credits_per_answer"] < board["baseline"]["credits_per_answer"]
    assert board["tally"]["conflicts_caught"] == 1 and board["baseline"]["conflicts_caught"] == 0
    assert tally.credits + naive.credits == pytest.approx(world.credits_spent)


async def test_a_top_up_continues_where_the_budget_ran_out(settings, fake, ledger):
    run = Run(settings, JOB, 3.0, minutes=20, transport=fake.transport(), today=TODAY, ledger=ledger)
    small = await run.execute()
    answered_before = sum(a.status is not Status.NOT_REACHED for a in small.answers)

    bigger = await run.top_up(50.0)
    answered_after = sum(a.status is not Status.NOT_REACHED for a in bigger.answers)
    assert answered_after > answered_before
    assert bigger.credits == pytest.approx(fake.credits_spent) and bigger.credits <= 53.0


async def test_a_cut_survives_a_restart(settings, fake, ledger):
    run = Run(settings, JOB, 400.0, minutes=20, run_id="cut-then-crash", transport=fake.transport(),
              today=TODAY, ledger=ledger)
    run.wallet.cut(5.0)
    resumed = Run(settings, JOB, 400.0, minutes=20, run_id="cut-then-crash", transport=fake.transport(),
                  today=TODAY, ledger=ledger)
    assert resumed.wallet.root.limit == 5.0


async def test_the_vendors_stale_list_is_caught_from_our_own_receipts(settings, fake, ledger):
    from tally.vendor import audit_offer, make_offer

    result = await run_job(settings, fake, ledger)
    spent = fake.credits_spent
    audit = audit_offer(make_offer(result.answers), result.answers, sample_size=7)

    verdicts = {c.record.company: c.verdict for c in audit.checks}
    assert verdicts["Upitra"] == "left"  # the vendor still lists the head who left
    assert list(verdicts.values()).count("wrong person") == 2  # the two records it mixed up
    assert audit.accuracy < 0.9 and not audit.decision.startswith("recommend")
    assert fake.credits_spent == spent  # checking the sample cost nothing


async def test_the_quote_spends_only_its_capped_sample_and_the_rest_waits_for_accept(settings, fake, ledger):
    budget = 15.0
    run = Run(settings, JOB, budget, minutes=20, transport=fake.transport(), today=TODAY, ledger=ledger)
    quote = await run.quote()
    spent_on_quote = ledger.credits_charged(run.run_id)
    assert 0 < spent_on_quote <= quote_cap(budget)  # never more than the button said
    assert len(run.cases) < run.job_size == quote.companies == 8  # a sample, priced for the whole job

    result = await run.deliver()  # what Accept starts
    assert ledger.credits_charged(run.run_id) > spent_on_quote
    assert result.credits <= budget
    assert len(result.answers) == 8  # the rest was listed only after Accept


async def test_a_client_list_is_looked_up_a_sample_first(settings, fake, ledger):
    job = "Head of engineering at paysetu.in, ledgerline.com, finvolt.in, upitra.com and coinwell.in"
    run = Run(settings, job, 10.0, minutes=20, transport=fake.transport(), today=TODAY, ledger=ledger)
    await run.quote()
    assert ledger.credits_charged(run.run_id) <= quote_cap(10.0)
    result = await run.deliver()
    assert sorted(a.company.name for a in result.answers) == [
        "Coinwell", "Finvolt", "Ledgerline", "Paysetu Technologies", "Upitra"]
