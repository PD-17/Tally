"""The deliverable: a ranked table, a receipt for every row, and the bill."""

from __future__ import annotations

from tally.config import Settings
from tally.ledger import Ledger
from tally.models import Answer
from tally.runner import RunResult

STATUS_LABEL = {
    "confirmed": "Confirmed", "probable": "Probable", "conflict": "Conflict", "cant_verify": "Can't verify",
    "held": "Held for a person", "not_reached": "Not reached",
}


def receipt(answer: Answer, ledger: Ledger) -> dict:
    """What one row cost, which calls bought it, and where each claim comes from."""
    calls = [{"call_key": p.call_key[:16], "endpoint": p.endpoint, "request_id": p.request_id,
              "credits_for_this_row": round(p.credits, 4)} for p in answer.purchases]
    return {
        "company": answer.company.name,
        "status": answer.status.value,
        "answer": None if answer.person is None else {
            "name": answer.person.name, "title": answer.person.title, "since": answer.person.start_date,
            "profile": answer.person.profile_url,
        },
        "reason": answer.reason,
        "sources": [{"kind": s.kind, "ref": s.ref, "field": s.field, "says": s.value, "dated": s.record_date}
                    for s in answer.sources],
        "calls": calls,
    }


def bill(result: RunResult, ledger: Ledger, settings: Settings) -> dict:
    credits = ledger.credits_charged(result.run_id)
    data_inr = settings.credits_to_inr(credits)
    model_inr = result.model_cost_inr
    total = None if data_inr is None or model_inr is None else round(data_inr + model_inr, 2)
    answered = sum(a.status.value in ("confirmed", "probable", "conflict") for a in result.answers)
    return {
        "credits": credits,
        "data_inr": None if data_inr is None else round(data_inr, 2),
        "model_inr": model_inr,
        "total_inr": total,
        "answered": answered,
        "companies": len(result.answers),
        "credits_per_answer": round(credits / answered, 3) if answered else None,
        "ledger_chain_ok": ledger.verify_chain().ok,
        "stopped_because": result.stop_reason,
    }


def as_markdown(result: RunResult, ledger: Ledger, settings: Settings) -> str:
    totals = bill(result, ledger, settings)
    inr = lambda v: "-" if v is None else f"₹{v:,.2f}"  # noqa: E731
    lines = [
        f"# Tally run {result.run_id}",
        "",
        f"**{totals['answered']} of {totals['companies']} answered** for {totals['credits']:.2f} credits "
        f"({inr(totals['data_inr'])} of data + {inr(totals['model_inr'])} of model calls = "
        f"{inr(totals['total_inr'])}). Stopped because: {totals['stopped_because']}. "
        f"Ledger hash chain: {'verified' if totals['ledger_chain_ok'] else 'BROKEN'}.",
        "",
        "| Company | Status | Person | Title | Since | Why | Credits |",
        "|---|---|---|---|---|---|---|",
    ]
    for answer in result.answers:
        person = answer.person
        lines.append(
            f"| {answer.company.name} | {STATUS_LABEL[answer.status.value]} | {person.name if person else '-'} "
            f"| {person.title if person else '-'} | {(person.start_date or '-') if person else '-'} "
            f"| {answer.reason} | {answer.credits:.2f} |"
        )
    lines += ["", "## Sources", ""]
    for answer in result.answers:
        for source in answer.sources:
            lines.append(f"- **{answer.company.name}**: {source.field} = \"{source.value}\" "
                         f"({source.ref}, dated {source.record_date or 'unknown'})")
    return "\n".join(lines) + "\n"
