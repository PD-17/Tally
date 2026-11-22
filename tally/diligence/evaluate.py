"""`tally diligence-eval sealed/answers.json`: run every sealed deck and score the memos.

The answer file (written by whoever wrote the decks, never by the builder):

    {"decks": [{"file": "acme.pdf", "domain": "acme.in", "founders": ["Asha Rao"],
                "altered_claims": [{"kind": "headcount", "deck_text": "a team of 60"}],
                "lookalike_domain": false, "hidden_instruction": false}]}

Scored the way 2.2's tests read: every altered claim comes back contradicted with a source,
no other claim comes back contradicted, the lookalike domain is flagged, and the hidden
instruction is flagged (and, since verdicts come from rules, changes nothing).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from tally.config import Settings
from tally.diligence.claims import Verdict
from tally.diligence.deck import load_deck
from tally.diligence.run import DiligenceRun


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def matches(result, altered: dict) -> bool:
    if altered.get("kind") and altered["kind"] != result.claim.kind.value:
        return False
    a, b = _words(altered.get("deck_text", "")), _words(result.claim.text)
    if not a:
        return True
    return a <= b or b <= a or len(a & b) / len(a | b) >= 0.5


async def evaluate(settings: Settings, answers_path: Path, budget: float = 150, minutes: float = 8) -> int:
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    rows, totals = [], {"altered": 0, "caught": 0, "false_contradictions": 0, "flags_needed": 0, "flags_ok": 0}
    for entry in answers["decks"]:
        deck = load_deck(answers_path.parent / entry["file"])
        started = time.time()
        run = DiligenceRun(settings, deck, budget, minutes, founders=entry.get("founders"),
                           domain=entry.get("domain"))
        memo = await run.execute()
        seconds = time.time() - started
        altered = entry.get("altered_claims", [])
        caught = [a for a in altered if any(matches(r, a) and r.verdict is Verdict.CONTRADICTED and r.sources
                                            for r in memo.results)]
        false = [r for r in memo.results if r.verdict is Verdict.CONTRADICTED
                 and not any(matches(r, a) for a in altered)]
        flags = []
        if entry.get("lookalike_domain"):
            flags.append(("lookalike flagged", bool(memo.domain_flag)))
        if entry.get("hidden_instruction"):
            flags.append(("instruction flagged", bool(memo.instructions)))
        totals["altered"] += len(altered)
        totals["caught"] += len(caught)
        totals["false_contradictions"] += len(false)
        totals["flags_needed"] += len(flags)
        totals["flags_ok"] += sum(ok for _, ok in flags)
        rows.append({"deck": entry["file"], "run_id": memo.run_id, "credits": memo.credits,
                     "seconds": round(seconds, 1), "altered": len(altered), "caught": len(caught),
                     "missed": [a.get("deck_text") for a in altered if a not in caught],
                     "false_contradictions": [r.claim.text for r in false],
                     "flags": {name: ok for name, ok in flags}})
        print(f"{entry['file']:<28} caught {len(caught)}/{len(altered)} altered, {len(false)} false contradictions, "
              f"{memo.credits:.2f} credits, {seconds:.0f}s"
              + "".join(f", {name}: {'yes' if ok else 'NO'}" for name, ok in flags))
    print(f"\naltered claims contradicted: {totals['caught']}/{totals['altered']}; false contradictions: "
          f"{totals['false_contradictions']}; flags: {totals['flags_ok']}/{totals['flags_needed']}")
    out = settings.data_dir / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"totals": totals, "decks": rows}, indent=2), encoding="utf-8")
    print(f"details: {out}")
    return 0
