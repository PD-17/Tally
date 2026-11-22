"""Settings, read once from the environment (and a .env file if present).

Every number that turns credits or tokens into money lives here, so the
quote, the ledger and the report all use the same rates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Mode = Literal["live", "record", "replay", "fake"]

# Claude list prices in USD per million tokens (input, output), from the
# Anthropic SDK docs for these model ids.
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
}

SMART_MODEL = "claude-opus-5-5"  # reading the deck

PRICE_MULTIPLE = 3.0  # price = cost to serve x this; set by the business, shown on the quote


@dataclass(frozen=True)
class Settings:
    crustdata_api_key: str | None
    mode: Mode
    data_dir: Path
    usd_per_credit: float
    inr_per_usd: float | None
    base_url: str = "https://api.crustdata.com"
    api_version: str = "2025-11-01"

    @property
    def replay_dir(self) -> Path:
        return self.data_dir / "responses"

    def credits_to_inr(self, credits: float) -> float | None:
        """Rupee value of some credits, or None until the exchange rate is set."""
        if self.inr_per_usd is None:
            return None
        return credits * self.usd_per_credit * self.inr_per_usd

    def tokens_to_inr(self, model: str, tokens_in: int, tokens_out: int) -> float | None:
        """Rupee cost of one model call at list price."""
        prices = MODEL_PRICES_USD_PER_MTOK.get(model)
        if prices is None or self.inr_per_usd is None:
            return None
        usd = (tokens_in * prices[0] + tokens_out * prices[1]) / 1_000_000
        return usd * self.inr_per_usd


def load_settings(**overrides) -> Settings:
    """Build Settings from the environment; keyword arguments win over it."""
    load_dotenv()
    inr = os.getenv("TALLY_INR_PER_USD", "").strip()
    values = dict(
        crustdata_api_key=os.getenv("CRUSTDATA_API_KEY") or None,
        mode=os.getenv("TALLY_MODE", "live"),
        data_dir=Path(os.getenv("TALLY_DATA_DIR", "data")),
        usd_per_credit=float(os.getenv("TALLY_USD_PER_CREDIT", "0.30")),
        inr_per_usd=float(inr) if inr else None,
    )
    values.update(overrides)
    if values["mode"] not in ("live", "record", "replay", "fake"):
        raise ValueError(f"TALLY_MODE must be live, record, replay or fake, not {values['mode']!r}")
    return Settings(**values)
