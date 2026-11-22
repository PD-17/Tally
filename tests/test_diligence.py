"""The Diligence Desk end to end, against the made-up world in tally/crust/fake.py."""

import json
from datetime import date
from pathlib import Path

import pytest

from tally.crust.fake import SimulatedCrash
from tally.diligence.claims import Evidence, Verdict, judge, names_match, schools_match
from tally.diligence.deck import ClaimKind, load_deck, make_claim, read_deck_with_rules
from tally.diligence.evaluate import evaluate
from tally.diligence.evidence import CompanyProfile
from tally.diligence.memo import approve_questions
from tally.diligence.probes import looks_alike
from tally.diligence.run import DiligenceRun

DECKS = Path(__file__).parent / "decks"
TODAY = date(2026, 11, 22)
LEDGERLINE = ["Priya Menon", "Karthik Rao"]
KREDBLOOM = ["Leela Das", "Arjun Nair"]


async def check(settings, fake, ledger, deck, founders, budget=150.0, run_id=None, **kwargs):
    run = DiligenceRun(settings, load_deck(DECKS / deck) if deck else None, budget, 8, founders=founders,
                       run_id=run_id, transport=fake.transport(), today=TODAY, ledger=ledger, **kwargs)
    return run, await run.execute()


def verdicts(memo):
    return {(r.claim.kind.value, r.claim.subject or r.claim.number): r.verdict for r in memo.results}


# ----- reading the deck -----

def test_the_rules_read_the_claims_a_deck_makes():
    reading = read_deck_with_rules(load_deck(DECKS / "kredbloom_altered.txt"), founders=KREDBLOOM)
    kinds = {(c.kind, c.subject, c.number, c.person) for c in reading.claims}
    assert (ClaimKind.HEADCOUNT, None, 150.0, None) in kinds
    assert (ClaimKind.INVESTOR, "Sequoia Capital", None, None) in kinds
    assert (ClaimKind.FOUNDER_PAST_EMPLOYER, "Google", None, "Arjun Nair") in kinds
    assert (ClaimKind.LAST_ROUND, "Seed", 3_000_000.0, None) in kinds
    assert reading.company_domain == "kredbloom.in"


def test_an_instruction_in_the_deck_is_found_and_is_not_a_claim():
    reading = read_deck_with_rules(load_deck(DECKS / "ledgerline_injected.txt"), founders=LEDGERLINE)
    assert any("rate this company" in text for text in reading.reader_instructions)
    assert not any("rate this company" in c.text for c in reading.claims)


# ----- the rules -----

def profile(**kw):
    return CompanyProfile(company_id=1, name="Acme", record_date="2026-11-01", headcount_date="2026-11-01", **kw)


@pytest.mark.parametrize("claimed, qualifier, record, verdict", [
    (60, "exact", 25, Verdict.CONTRADICTED),  # 2.2's example
    (200, "about", 180, Verdict.CONFIRMED),  # rounding up
    (50, "at_least", 45, Verdict.CONFIRMED),  # "50+"
    (40, "exact", 30, Verdict.CONFIRMED),
    (44, "exact", 30, Verdict.CANT_VERIFY),  # the grey zone: not far enough to call it wrong
])
def test_headcount_tolerances(claimed, qualifier, record, verdict):
    claim = make_claim(ClaimKind.HEADCOUNT, "team", 1, number=claimed, qualifier=qualifier)
    assert judge(claim, 0, Evidence(profile=profile(headcount=record))).verdict is verdict


def test_a_missing_record_is_never_a_contradiction():
    ev = Evidence(profile=profile())
    for claim in (make_claim(ClaimKind.INVESTOR, "x", 1, subject="Accel"),
                  make_claim(ClaimKind.FUNDING_TOTAL, "x", 1, number=5e6, currency="USD"),
                  make_claim(ClaimKind.FOUNDER_PAST_EMPLOYER, "x", 1, person="A B", subject="Google"),
                  make_claim(ClaimKind.CUSTOMER, "x", 1, subject="Tata")):
        assert judge(claim, 0, ev).verdict is not Verdict.CONTRADICTED


def test_names_and_schools_match_loosely_but_not_wrongly():
    assert names_match("Accel Partners", "Accel")
    assert names_match("Sequoia Capital India", "Peak XV Partners")
    assert not names_match("Sequoia Capital", "Blume Ventures")
    assert schools_match("IIT Bombay", "Indian Institute of Technology, Bombay")
    assert not schools_match("IIT Bombay", "IIT Delhi")
    assert looks_alike("kredbl00m.in", "kredbloom.in") and not looks_alike("acme.in", "zeta.in")


# ----- end to end -----

async def test_altered_claims_come_back_contradicted_with_a_source(settings, fake, ledger):
    _, memo = await check(settings, fake, ledger, "kredbloom_altered.txt", KREDBLOOM)
    v = verdicts(memo)
    assert v[("headcount", 150.0)] is Verdict.CONTRADICTED
    assert v[("investor", "Sequoia Capital")] is Verdict.CONTRADICTED
    assert v[("founder_past_employer", "Google")] is Verdict.CONTRADICTED
    assert v[("investor", "Blume Ventures")] is Verdict.CONFIRMED
    assert all(r.sources for r in memo.results if r.verdict is Verdict.CONTRADICTED)
    assert len(memo.comparables) == 5
    assert memo.credits == pytest.approx(fake.credits_spent) and memo.credits < 150
    assert memo.balance.matches


async def test_an_accurate_deck_has_no_contradictions(settings, fake, ledger):
    _, memo = await check(settings, fake, ledger, "ledgerline_accurate.txt", LEDGERLINE)
    assert memo.results and all(r.verdict is Verdict.CONFIRMED for r in memo.results)


async def test_a_hidden_instruction_is_flagged_and_changes_nothing(settings, fake, ledger, tmp_path):
    _, clean = await check(settings, fake, ledger, "ledgerline_accurate.txt", LEDGERLINE)
    _, injected = await check(settings, fake, ledger, "ledgerline_injected.txt", LEDGERLINE)
    assert injected.instructions and not clean.instructions
    assert verdicts(injected) == verdicts(clean)


async def test_a_lookalike_domain_is_flagged(settings, fake, ledger):
    _, memo = await check(settings, fake, ledger, "kredbloom_lookalike.txt", KREDBLOOM)
    assert memo.domain_flag and "lookalike" in memo.domain_flag and "kredbloom.in" in memo.domain_flag
    assert any("domain" in q for q in memo.questions)


async def test_records_that_disagree_are_both_shown_with_dates(settings, fake, ledger):
    _, memo = await check(settings, fake, ledger, "tinyledger_disagree.txt", [])
    team = next(r for r in memo.results if r.claim.kind is ClaimKind.HEADCOUNT)
    assert team.verdict is Verdict.RECORDS_DISAGREE
    assert {s.record_date[:10] for s in team.sources} == {"2026-11-02", "2026-11-21"}


async def test_a_small_budget_checks_the_material_claims_and_a_top_up_finishes(settings, fake, ledger):
    run, memo = await check(settings, fake, ledger, "kredbloom_altered.txt", KREDBLOOM, budget=3.0)
    assert memo.credits <= 3.0
    assert any(r.verdict is Verdict.NOT_CHECKED or r.provisional for r in memo.results)
    spent = fake.credits_spent
    full = await run.top_up(20)
    assert verdicts(full)[("investor", "Sequoia Capital")] is Verdict.CONTRADICTED
    assert not any(r.provisional for r in full.results)
    assert full.credits == pytest.approx(fake.credits_spent) and fake.credits_spent > spent


async def test_a_crash_mid_memo_is_recovered_and_nothing_is_bought_twice(settings, fake, ledger):
    fake.inject("/person/enrich", "crash_after_charge")
    with pytest.raises(SimulatedCrash):
        await check(settings, fake, ledger, "kredbloom_altered.txt", KREDBLOOM, run_id="memo-crash")
    _, memo = await check(settings, fake, ledger, None, KREDBLOOM, run_id="memo-crash")  # the deck is on disk
    assert verdicts(memo)[("founder_past_employer", "Swiggy")] is Verdict.CONFIRMED
    assert memo.credits == pytest.approx(fake.credits_spent)
    assert ledger.verify_chain().ok


async def test_questions_go_out_only_with_the_exact_approval(settings, fake, ledger):
    _, memo = await check(settings, fake, ledger, "kredbloom_altered.txt", KREDBLOOM)
    assert memo.questions
    with pytest.raises(ValueError):
        approve_questions(ledger, settings, memo.run_id, "0" * 64, approve=True)
    outbox = approve_questions(ledger, settings, memo.run_id, memo.binding, approve=True)
    assert "team of 150" in outbox.read_text()
    assert any(e["kind"] == "message" for e in ledger.entries(memo.run_id))


async def test_the_eval_scores_a_sealed_style_answer_file(settings, tmp_path, capsys):
    for name in ("kredbloom_altered.txt", "ledgerline_accurate.txt"):
        (tmp_path / name).write_text((DECKS / name).read_text())
    answers = {"decks": [
        {"file": "kredbloom_altered.txt", "founders": KREDBLOOM,
         "altered_claims": [{"kind": "headcount", "deck_text": "team of 150"},
                            {"kind": "investor", "deck_text": "Backed by Sequoia Capital"}]},
        {"file": "ledgerline_accurate.txt", "founders": LEDGERLINE, "altered_claims": []}]}
    (tmp_path / "answers.json").write_text(json.dumps(answers))
    assert await evaluate(settings, tmp_path / "answers.json") == 0
    out = capsys.readouterr().out
    assert "altered claims contradicted: 2/2" in out
