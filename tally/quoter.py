"""The quote: what the client's budget will buy, before most of it is spent.

Tally first does the job properly for a few pilot companies (real purchases,
which the full run keeps). Each pilot company tells us what one company
really costs and whether it ended up answered. The quote then resamples those
real outcomes thousands of times for the remaining companies:

    credits needed  typical (P50) and high-end (P90)
    answers         expected at the client's budget
    coverage curve  expected answers for every budget, up to twice the client's
    time and money  minutes, our cost in rupees, and the price we'd charge

It is a forecast from a small sample, and the quote card says so.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from tally.config import Settings

SIMULATED_RUNS = 2000
PRICE_MULTIPLE = 3.0  # price = cost to serve x this; set by the business, shown on the quote


@dataclass(frozen=True)
class PilotCompany:
    """What doing one company properly actually took."""

    credits: float
    answered: bool  # confirmed, probable or conflict
    seconds: float
    model_inr: float = 0.0


@dataclass
class Quote:
    companies: int
    budget: float
    p50_credits: float
    p90_credits: float
    expected_answers: float  # if the budget were unlimited
    expected_answers_at_budget: float
    curve: list[tuple[float, float]] = field(default_factory=list)  # (budget, expected answers)
    minutes: float = 0.0
    cost_inr: float | None = None
    price_inr: float | None = None
    pilot_size: int = 0

    def as_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()
                if k != "curve"} | {"curve": [[round(b, 1), round(a, 2)] for b, a in self.curve]}

    def headline(self) -> str:
        inr = "-" if self.price_inr is None else f"₹{self.price_inr:,.0f}"
        duration = "under a minute" if self.minutes < 1 else f"about {self.minutes:.0f} minutes"
        return (f"For {self.budget:.0f} credits, expect about {self.expected_answers_at_budget:.0f} of "
                f"{self.companies} answered, using about {self.p50_credits:.0f} credits "
                f"(up to {self.p90_credits:.0f}), in {duration}. Price {inr}. "
                f"Forecast from {self.pilot_size} pilot companies.")


def make_quote(pilot: list[PilotCompany], companies: int, budget: float, already_spent: float,
               settings: Settings, seed: int = 7) -> Quote:
    """Resample the pilot to forecast the whole job. `already_spent` covers listing and the pilot."""
    if not pilot:
        raise ValueError("a quote needs at least one pilot company")
    rng = random.Random(seed)  # fixed seed: the same pilot always gives the same quote
    remaining = max(companies - len(pilot), 0)
    pilot_answered = sum(p.answered for p in pilot)

    totals, answers = [], []
    for _ in range(SIMULATED_RUNS):
        sample = [rng.choice(pilot) for _ in range(remaining)]
        totals.append(already_spent + sum(p.credits for p in sample))
        answers.append(pilot_answered + sum(p.answered for p in sample))
    totals.sort()
    typical, high = totals[len(totals) // 2], totals[int(len(totals) * 0.9)]

    # The curve spans where the money matters: up to twice the budget, or 1.5x the high end if
    # the whole job costs much less than the budget.
    top = 2 * budget if high > budget / 2 else 1.5 * high
    budgets = [already_spent + (top - already_spent) * step / 20 for step in range(21)]
    curve = [(b, _answers_within(pilot, remaining, b - already_spent, pilot_answered, rng)) for b in budgets]
    seconds_per_company = sum(p.seconds for p in pilot) / len(pilot)
    data_inr = settings.credits_to_inr(typical)
    model_inr = sum(p.model_inr for p in pilot) / len(pilot) * companies
    cost_inr = None if data_inr is None else data_inr + model_inr
    return Quote(
        companies=companies, budget=budget, p50_credits=typical, p90_credits=high,
        expected_answers=sum(answers) / len(answers),
        expected_answers_at_budget=_answers_within(pilot, remaining, budget - already_spent, pilot_answered, rng),
        curve=curve, minutes=seconds_per_company * remaining / 60, cost_inr=cost_inr,
        price_inr=None if cost_inr is None else cost_inr * PRICE_MULTIPLE, pilot_size=len(pilot),
    )


def _answers_within(pilot: list[PilotCompany], remaining: int, money: float, pilot_answered: int,
                    rng: random.Random, runs: int = 400) -> float:
    """Expected answers when companies are done in turn until `money` runs out."""
    total = 0
    for _ in range(runs):
        left, answered = money, pilot_answered
        for _ in range(remaining):
            company = rng.choice(pilot)
            if company.credits > left:
                break
            left -= company.credits
            answered += company.answered
        total += answered
    return total / runs
