"""Command line: `tally run "<job>" --budget 400`, `tally verify`, `tally ledger <run id>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tally.config import load_settings
from tally.ledger import Ledger
from tally.report import as_markdown, bill, receipt
from tally.runner import Run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tally", description="Research that quotes, delivers and itemises.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run a research job")
    run.add_argument("job", help='the job in plain words, e.g. "head of engineering at 60 Indian fintech companies"')
    run.add_argument("--budget", type=float, required=True, help="credit limit for the run")
    run.add_argument("--minutes", type=float, default=20, help="time limit for the run (default 20)")
    run.add_argument("--run-id", help="resume this run (everything already bought is free)")
    run.add_argument("--mode", choices=["live", "record", "replay", "fake"], help="overrides TALLY_MODE")
    run.add_argument("--out", type=Path, help="write the report here (.md) and the receipts beside it (.json)")

    serve = commands.add_parser("serve", help="open the desk and phone dial at http://localhost:8000")
    serve.add_argument("--mode", choices=["live", "record", "replay", "fake"], help="overrides TALLY_MODE")
    serve.add_argument("--port", type=int, default=8000)

    wallet = commands.add_parser("wallet", help="run Tally Wallet, an MCP server (stdio) for any agent")
    wallet.add_argument("--cap", type=float, required=True, help="credits the agent may spend in total")
    wallet.add_argument("--mode", choices=["live", "fake"], help="overrides TALLY_MODE")

    probe = commands.add_parser("check", help="test the live API once the key works (about 0.45 credits)")
    probe.add_argument("--domain", default="crustdata.com")
    probe.add_argument("--mode", choices=["live", "fake"], help="fake: a dry run against made-up data, no key needed")
    probe.add_argument("--full", action="store_true",
                       help="also try Company Search, Company Enrich and Web Search (about 3.7 credits more)")

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

    if args.command == "run":
        return asyncio.run(_run(args, settings))
    if args.command == "wallet":
        from tally.wallet_mcp import serve

        serve(args.cap, settings)
        return 0
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


async def _run(args, settings) -> int:
    def show(kind: str, data: dict) -> None:
        if kind == "note":
            print(f"  - {data['text']}", file=sys.stderr)
        elif kind == "settled":
            print(f"    paid {data['credits']:.2f} for {data['endpoint']} "
                  f"(spent {data['spent']:.2f} of {data['limit']:.2f})", file=sys.stderr)

    job = Run(settings, args.job, args.budget, args.minutes, run_id=args.run_id, on_event=show)
    result = await job.execute()
    report = as_markdown(result, job.ledger, settings)
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        receipts = {"bill": bill(result, job.ledger, settings),
                    "rows": [receipt(a, job.ledger) for a in result.answers]}
        args.out.with_suffix(".json").write_text(json.dumps(receipts, indent=2), encoding="utf-8")
    print(f"run id: {result.run_id}  (resume with --run-id {result.run_id})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
