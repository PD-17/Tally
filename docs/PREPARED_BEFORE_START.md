# Written before building began (10:15 AM, 22 Nov 2026)

The submission form asks "Anything written before 10:15 AM?". This is the full answer. The git history starts after 10:15, so everything below arrives in the first commit.

## Design (written 19–22 Nov)
- `docs/system-design.md`: the Tally system design.
- `docs/build-plan.md`: the build plan, credit estimates and schedule.

## Code (written on 22 Nov, from about 8:45 AM to 9:30 AM)
Written with an AI coding assistant, and tested only against an offline stand-in for Crustdata (`tally/crust/fake.py`, made-up data). No live Crustdata call had been made, because credits were handed out at 9:45.

| Level | What | Files |
|---|---|---|
| 0 | Project setup | `pyproject.toml`, `.env.example`, `.gitignore`, `README.md` |
| 1 | The money path: ledger, wallet, Crustdata client | `tally/config.py`, `tally/ledger.py`, `tally/wallet.py`, `tally/crust/*` |
| 2 | One job end to end | `tally/models.py`, `tally/prices.py`, `tally/probes.py`, `tally/verifier.py`, `tally/llm.py`, `tally/intake.py`, `tally/planner.py`, `tally/runner.py`, `tally/report.py`, `tally/cli.py` |
| 3 | Quote, crash recovery, race, desk and phone | `tally/quoter.py`, `tally/recovery.py`, `tally/race.py`, `tally/events.py`, `tally/api.py`, `tally/static/*` |
| 4 | Top-up, vendor audit, Wallet MCP, live check | `tally/vendor.py`, `tally/wallet_mcp.py`, `tally/check.py` |
| | Tests (43, all offline) | `tests/*` |

## Done after 10:15
Everything in the git history after the first commit: the first live calls, fixes to response shapes, live test cases and results, the demo video.

## After 10:15: the switch to 2.2
At about 1 PM the project moved from statement 7.1 (The Research Buyer) to 2.2 (The Diligence Desk), keeping the spending engine (wallet, ledger, recovery, Crustdata client). The 7.1-only code and the two design documents above were removed from the tree at the end of the day so the repo shows only what is submitted; they remain in the git history (first commit `ca93f82`).
