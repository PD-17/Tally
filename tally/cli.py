"""Command line: `tally diligence deck.pdf`, `tally diligence-eval`, `tally serve`, `tally verify`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tally.config import load_settings
from tally.ledger import Ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tally", description="Every pitch-deck claim checked against records, inside a budget.")
    commands = parser.add_subparsers(dest="command", required=True)

    deck = commands.add_parser("diligence", help="check a pitch deck's claims and write the memo")
    deck.add_argument("deck", type=Path, help="the deck: a PDF, or a text file with '---' between pages")
    deck.add_argument("--founders", default="", help='the founders\' names, comma separated: "Asha Rao, Vik Mehta"')
    deck.add_argument("--domain", help="the company's domain, if the partner knows it")
    deck.add_argument("--budget", type=float, default=150, help="credit limit for this memo (default 150)")
    deck.add_argument("--minutes", type=float, default=8, help="time limit (default 8)")
    deck.add_argument("--run-id", help="resume this memo (everything already bought is free)")
    deck.add_argument("--mode", choices=["live", "record", "replay", "fake"], help="overrides TALLY_MODE")
    deck.add_argument("--out", type=Path, help="write the memo here (.md) and the receipts beside it (.json)")

    evaluate = commands.add_parser("diligence-eval", help="score memos against a sealed answer file")
    evaluate.add_argument("answers", type=Path, help="sealed/answers.json")
    evaluate.add_argument("--budget", type=float, default=150)
    evaluate.add_argument("--minutes", type=float, default=8)
    evaluate.add_argument("--mode", choices=["live", "record", "replay", "fake"], help="overrides TALLY_MODE")

    serve = commands.add_parser("serve", help="open the desk and phone dial at http://localhost:8000")
    serve.add_argument("--mode", choices=["live", "record", "replay", "fake"], help="overrides TALLY_MODE")
    serve.add_argument("--port", type=int, default=8000)

    probe = commands.add_parser("check", help="test the live API once the key works (about 2.4 credits)")
    probe.add_argument("--domain", default="razorpay.com")
    probe.add_argument("--mode", choices=["live", "fake"], help="fake: a dry run against made-up data, no key needed")
    probe.add_argument("--full", action="store_true",
                       help="also try Job Search, Company Search and Web Search (about 5 credits more)")

    commands.add_parser("verify", help="check the ledger's hash chain")
    show = commands.add_parser("ledger", help="print a run's ledger entries")
    show.add_argument("run_id")

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # the Windows console defaults to a code page without ₹
    settings = load_settings(**({"mode": args.mode} if getattr(args, "mode", None) else {}))
    if settings.mode == "fake":
        print("FAKE MODE: made-up offline data, for tests only. Results are not real.", file=sys.stderr)
    if settings.mode == "replay":
        print("REPLAY: serving saved responses, not live data.", file=sys.stderr)

    if args.command == "diligence":
        return asyncio.run(_diligence(args, settings))
    if args.command == "diligence-eval":
        from tally.diligence.evaluate import evaluate

        return asyncio.run(evaluate(settings, args.answers, args.budget, args.minutes))
    if args.command == "check":
        from tally.check import check

        return 1 if asyncio.run(check(settings, args.domain, full=args.full)) else 0
    if args.command == "serve":
        import uvicorn

        from tally.api import create_app

        # 0.0.0.0 so a phone on the same Wi-Fi can open the dial (Windows may ask to allow it).
        uvicorn.run(create_app(settings), host="0.0.0.0", port=args.port)
        return 0
    ledger = Ledger(settings.data_dir / "ledger.sqlite")
    if args.command == "verify":
        check = ledger.verify_chain()
        print(f"{check.entries} entries; chain {'verified' if check.ok else f'BROKEN at entry {check.first_bad_seq}'}")
        return 0 if check.ok else 1
    for entry in ledger.entries(args.run_id):
        print(f"{entry['seq']:>5} {entry['event']:<9} {entry['kind']:<9} {entry['endpoint'] or entry['reason']!s:<28} "
              f"{entry['credits'] if entry['credits'] is not None else '':>8} {entry['request_id'] or ''}")
    return 0


def _show(kind: str, data: dict) -> None:
    if kind == "note":
        print(f"  - {data['text']}", file=sys.stderr)
    elif kind == "settled":
        print(f"    paid {data['credits']:.2f} for {data['endpoint']} "
              f"(spent {data['spent']:.2f} of {data['limit']:.2f})", file=sys.stderr)


async def _diligence(args, settings) -> int:
    from tally.diligence.deck import load_deck
    from tally.diligence.memo import as_dict, as_markdown
    from tally.diligence.run import DiligenceRun

    founders = [f.strip() for f in args.founders.split(",") if f.strip()]
    deck = load_deck(args.deck) if args.deck.exists() else None
    job = DiligenceRun(settings, deck, args.budget, args.minutes, founders=founders, domain=args.domain,
                       run_id=args.run_id, on_event=_show)
    memo = await job.execute()
    text = as_markdown(memo, job.ledger, settings)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        args.out.with_suffix(".json").write_text(json.dumps(as_dict(memo, job.ledger, settings), indent=2,
                                                            default=str), encoding="utf-8")
    print(f"run id: {memo.run_id}  (resume with --run-id {memo.run_id})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
