"""The race: Tally and the naive enrich-everything buyer, same job, same budget, side by side.

Each has its own run, wallet and receipts. The scoreboard compares what a
client cares about: answers, credits per answer, and the stale facts each one
would have handed over as true.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tally.models import Status
from tally.planner import EnrichEverything
from tally.runner import ANSWERED, RunResult


@dataclass(frozen=True)
class Lane:
    name: str
    answered: int
    credits: float
    conflicts_caught: int  # people the record still lists, whom newer news says have left

    @property
    def credits_per_answer(self) -> float | None:
        return round(self.credits / self.answered, 3) if self.answered else None

    def as_dict(self) -> dict:
        return self.__dict__ | {"credits_per_answer": self.credits_per_answer}


def lane(name: str, result: RunResult) -> Lane:
    return Lane(name, sum(a.status in ANSWERED for a in result.answers), result.credits,
                sum(a.status is Status.CONFLICT for a in result.answers))


async def race(make_run, job_text: str, budget: float, minutes: float, run_id: str) -> tuple[RunResult, RunResult]:
    """Run both buyers at once. `make_run(**kwargs)` builds a Run with shared settings."""
    tally = make_run(job_text=job_text, budget=budget, minutes=minutes, run_id=run_id)
    naive = make_run(job_text=job_text, budget=budget, minutes=minutes, run_id=f"{run_id}-baseline",
                     planner=EnrichEverything)
    return await asyncio.gather(tally.execute(), naive.execute())


def scoreboard(tally: RunResult, naive: RunResult) -> dict:
    return {"tally": lane("Tally", tally).as_dict(), "baseline": lane("Enrich everything", naive).as_dict()}

