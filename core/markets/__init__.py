from dataclasses import dataclass


@dataclass
class MarketData:
    asset: str
    venue: str          # "polymarket" | "kalshi"
    horizon: str        # "5m" | "15m"
    up_ref: str         # Polymarket: token_id  |  Kalshi: market ticker
    down_ref: str
    implied_up: float
    implied_down: float
    window_close_ts: int
    fetched_at: str     # UTC ISO string


class MarketNotIndexed(Exception):
    """Raised when the expected market window has not been indexed yet."""
