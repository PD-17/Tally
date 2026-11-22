"""The deliverable: a memo with a verdict and a receipt for every claim, five comparables,
and questions for the founders that go out only after the partner approves that exact set."""

from __future__ import annotations

import time
from pathlib import Path

from tally.config import Settings
from tally.diligence.claims import Verdict, _money
from tally.diligence.planner import MATERIALITY
from tally.ledger import Ledger, canonical_json, sha256

MAX_QUESTIONS = 8
LABEL = {Verdict.CONFIRMED: "Confirmed", Verdict.CONTRADICTED: "Contradicted",
         Verdict.RECORDS_DISAGREE: "Records disagree", Verdict.CANT_VERIFY: "Can't verify",
         Verdict.HELD: "Held for a person", Verdict.NOT_CHECKED: "Not checked at this budget"}


# ----- questions --------------------------------------------------------------------------

def build_questions(results, domain_flag: str | None, company: str) -> list[str]:
    """Fixed templates from the verdicts and their evidence: nothing the deck says can write a question."""
    questions = []
    if domain_flag:
        questions.append(f"Your deck gives a different web domain from the one on record: {domain_flag} "
                         f"Which domain is yours?")
    ranked = sorted(results, key=lambda r: -MATERIALITY[r.claim.kind])
    for r in ranked:
        said = f"Your deck says \"{r.claim.text}\" (page {r.claim.page})."
        if r.verdict is Verdict.CONTRADICTED:
            questions.append(f"{said} Our records: {r.reason}. Can you walk us through the difference, with "
                             f"documents if you have them?")
        elif r.verdict is Verdict.RECORDS_DISAGREE:
            questions.append(f"{said} The records disagree: {r.reason}. Which is current?")
    for r in ranked:
        if r.verdict is Verdict.CANT_VERIFY and MATERIALITY[r.claim.kind] >= 0.8:
            questions.append(f"Your deck says \"{r.claim.text}\" (page {r.claim.page}). We couldn't find a record "
                             f"that shows this ({r.reason}). What can you share that would?")
    return questions[:MAX_QUESTIONS]


def questions_binding(run_id: str, questions: list[str]) -> str:
    """An approval is for these exact questions: change one word and it needs a new approval."""
    return sha256(f"{run_id}|{canonical_json(questions)}")


def approve_questions(ledger: Ledger, settings: Settings, run_id: str, binding: str, approve: bool,
                      actor: str = "partner") -> Path | None:
    """Record the partner's decision. Approved questions go to the team's outbox, never straight out."""
    run = ledger.run(run_id)
    details = (run or {}).get("details", {})
    questions = details.get("questions")
    if questions is None or binding != details.get("questions_binding"):
        raise ValueError("these aren't the questions waiting for approval")
    ledger.record_message(run_id, f"questions:{binding}", "approved" if approve else "denied",
                          note=f"{actor} {'approved' if approve else 'denied'} {len(questions)} questions")
    ledger.update_run(run_id, questions_decision="approved" if approve else "denied")
    if not approve:
        return None
    outbox = settings.data_dir / "outbox" / f"{run_id}.md"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Questions for the founders ({run['job_text']})", "",
             f"Approved by the {actor} at {time.strftime('%Y-%m-%d %H:%M')} (binding {binding[:16]}). "
             f"For the team to send; nothing was sent automatically.", ""]
    lines += [f"{i}. {q}" for i, q in enumerate(questions, start=1)]
    outbox.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return outbox


# ----- the memo -----------------------------------------------------------------------------

def receipt(r) -> dict:
    return {
        "claim": r.claim.text, "page": r.claim.page, "kind": r.claim.kind.value, "verdict": r.verdict.value,
        "reason": r.reason, "provisional": r.provisional,
        "quote_in_text_layer": r.quote_found,
        "sources": [{"kind": s.kind, "ref": s.ref, "field": s.field, "says": s.value, "dated": s.record_date}
                    for s in r.sources],
        "calls": [{"call_key": p.call_key[:16], "endpoint": p.endpoint, "request_id": p.request_id,
                   "credits_for_this_claim": round(p.credits, 4)} for p in r.purchases],
    }


def bill(memo, ledger: Ledger, settings: Settings) -> dict:
    credits = ledger.credits_charged(memo.run_id)
    data_inr = settings.credits_to_inr(credits)
    model_inr = memo.model_cost_inr
    counts = {v.value: sum(r.verdict is v for r in memo.results) for v in Verdict}
    return {"credits": credits, "data_inr": None if data_inr is None else round(data_inr, 2), "model_inr": model_inr,
            "total_inr": None if data_inr is None or model_inr is None else round(data_inr + model_inr, 2),
            "claims": len(memo.results), "verdicts": counts, "ledger_chain_ok": ledger.verify_chain().ok,
            "stopped_because": memo.stop_reason,
            "balance_matches": memo.balance.matches if memo.balance else None}


def as_dict(memo, ledger: Ledger, settings: Settings) -> dict:
    return {
        "run_id": memo.run_id, "company": memo.company, "domain_flag": memo.domain_flag,
        "instructions_ignored": memo.instructions, "founders": memo.reading.founders,
        "bill": bill(memo, ledger, settings), "quote": memo.quote,
        "claims": [receipt(r) for r in memo.results],
        "comparables": [c.__dict__ for c in memo.comparables],
        "questions": memo.questions, "questions_binding": memo.binding,
        "questions_decision": (ledger.run(memo.run_id) or {}).get("details", {}).get("questions_decision"),
    }


def as_markdown(memo, ledger: Ledger, settings: Settings) -> str:
    totals = bill(memo, ledger, settings)
    inr = lambda v: "-" if v is None else f"₹{v:,.2f}"  # noqa: E731
    v = totals["verdicts"]
    lines = [f"# Diligence memo: {memo.company}", "",
             f"**{len(memo.results)} claims**: {v['contradicted']} contradicted, {v['records_disagree']} where records "
             f"disagree, {v['cant_verify']} can't verify, {v['confirmed']} confirmed"
             + (f", {v['not_checked']} not checked at this budget" if v["not_checked"] else "")
             + (f", {v['held']} held" if v["held"] else "") + ".",
             f"Spent {totals['credits']:.2f} credits ({inr(totals['data_inr'])} of data + {inr(totals['model_inr'])} "
             f"of model calls). Stopped because: {totals['stopped_because']}. Ledger hash chain: "
             f"{'verified' if totals['ledger_chain_ok'] else 'BROKEN'}.", ""]
    if memo.domain_flag:
        lines += [f"> **Domain warning:** {memo.domain_flag}", ""]
    for text in memo.instructions:
        lines += [f"> **Instruction found in the deck, ignored:** \"{text}\". Verdicts are set by fixed rules; "
                  f"nothing in the deck can change them.", ""]
    lines += ["| # | The deck says (page) | Verdict | Why | Evidence (dated) | Credits |", "|---|---|---|---|---|---|"]
    for n, r in enumerate(memo.results, start=1):
        evidence = "; ".join(f"{s.field}: {s.value} ({(s.record_date or 'undated')[:10]})" for s in r.sources) or "-"
        label = LABEL[r.verdict] + (" (provisional)" if r.provisional and r.verdict is Verdict.CONTRADICTED else "")
        lines.append(f"| {n} | {_cell(r.claim.text)} (p.{r.claim.page}) | {label} | {_cell(r.reason)} | "
                     f"{_cell(evidence)} | {r.credits:.2f} |")
    lines += ["", "## Comparable companies", "", "| Company | Domain | Headcount | Total funding | Last round | "
              "Record dated |", "|---|---|---|---|---|---|"]
    for c in memo.comparables:
        lines.append(f"| {c.name} | {c.domain or '-'} | {c.headcount or '-'} | "
                     f"{_money(c.funding_total_usd) if c.funding_total_usd else '-'} | "
                     f"{c.last_round_type or '-'} {('(' + c.last_round_date + ')') if c.last_round_date else ''} | "
                     f"{c.record_date or '-'} |")
    if not memo.comparables:
        lines.append("| (none bought) | | | | | |")
    lines += ["", "## Questions for the founders (waiting for the partner's approval)", ""]
    lines += [f"{i}. {q}" for i, q in enumerate(memo.questions, start=1)] or ["(none)"]
    lines += ["", f"Approval binding: `{memo.binding[:16]}`", "", "## Sources", ""]
    for r in memo.results:
        for s in r.sources:
            lines.append(f"- **{r.claim.text[:60]}**: {s.field} = \"{s.value}\" ({s.ref}, dated "
                         f"{s.record_date or 'unknown'})")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")
