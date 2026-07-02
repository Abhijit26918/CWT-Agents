"""Kalshi market discovery + implied probability. MASTER_PLAN.md §2 Agent 1.

Public read-only — no auth required for market data.
Base URL: https://api.elections.kalshi.com/trade-api/v2

NOTE: Kalshi's shortest crypto up/down market is ~15-minute or hourly — NOT 5m.
We target the nearest available horizon (soonest-closing open event). The
Polymarket-vs-Kalshi horizon mismatch is logged explicitly and feeds the
cross-venue arbitrage check in Phase 5 (core/scale/arbitrage.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from core.markets import MarketData, MarketNotIndexed

logger = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
TIMEOUT = 10

# Known series tickers for BTC/ETH up/down — verified against Kalshi as of mid-2025.
# Update here if Kalshi renames/adds series.
ASSET_SERIES: dict[str, list[str]] = {
    "BTC": ["KXBTCD", "KXBTC"],
    "ETH": ["KXETHD", "KXETH"],
}


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict:
    url = f"{KALSHI_BASE}{path}"
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _iso_to_ts(iso_str: str) -> int:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Discovery chain: series → event → market
# ---------------------------------------------------------------------------

def _find_series_ticker(asset: str) -> str:
    """Return the first matching series ticker for *asset* from the live API."""
    data = _get("/series", {"category": "Crypto"})
    live_tickers = {s["ticker"] for s in data.get("series", [])}
    for candidate in ASSET_SERIES.get(asset, []):
        if candidate in live_tickers:
            logger.debug("Kalshi: using series ticker %s for %s", candidate, asset)
            return candidate
    raise MarketNotIndexed(
        f"No Kalshi series found for {asset}. "
        f"Checked: {ASSET_SERIES.get(asset, [])}. "
        f"Live tickers: {live_tickers}"
    )


def _find_soonest_event(series_ticker: str) -> dict:
    """Return the soonest-closing open event for the given series."""
    data = _get("/events", {"series_ticker": series_ticker, "status": "open"})
    events = data.get("events", [])
    if not events:
        raise MarketNotIndexed(
            f"No open events found for Kalshi series {series_ticker}"
        )
    # Real Kalshi API uses "strike_date"; fall back to "close_time" for older responses
    def _sort_key(e: dict) -> str:
        return e.get("strike_date") or e.get("close_time") or ""

    return min(events, key=_sort_key)


def _find_up_market(event_ticker: str) -> dict:
    """Return the most informative market for the given event.

    Kalshi crypto markets are price-level ladders ("Will BTC be above $X?").
    We select the market whose YES mid-price is closest to 0.50 — this is the
    'at the money' strike that best approximates P(up) for the upcoming window.
    """
    data = _get("/markets", {"event_ticker": event_ticker})
    markets = data.get("markets", [])
    if not markets:
        raise MarketNotIndexed(
            f"No markets found for Kalshi event {event_ticker}"
        )

    def _atm_distance(m: dict) -> float:
        yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid", 0) or 0)
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask", 1) or 1)
        mid = (yes_bid + yes_ask) / 2
        return abs(mid - 0.5)

    return min(markets, key=_atm_distance)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_market(asset: str, horizon: str = "5m") -> MarketData:
    """Find the soonest-closing up/down market for *asset* on Kalshi.

    Kalshi's shortest crypto up/down is ~15m/hourly — the *horizon* parameter
    from config is noted but Kalshi's actual window may be longer. The mismatch
    is logged for the cross-venue arbitrage feature.
    """
    series_ticker = _find_series_ticker(asset)
    event = _find_soonest_event(series_ticker)
    event_ticker = event.get("event_ticker") or event.get("ticker")
    market = _find_up_market(event_ticker)

    market_ticker = market["ticker"]
    # Real API: prices in "yes_bid_dollars"/"yes_ask_dollars" as 0-1 floats.
    # Fixture/older responses may use "yes_bid"/"yes_ask" as 0-100 integers.
    if "yes_bid_dollars" in market:
        yes_bid = float(market.get("yes_bid_dollars") or 0)
        yes_ask = float(market.get("yes_ask_dollars") or 1)
        implied_up = (yes_bid + yes_ask) / 2.0
    else:
        yes_bid = int(market.get("yes_bid", 0))
        yes_ask = int(market.get("yes_ask", 100))
        implied_up = (yes_bid + yes_ask) / 200.0
    implied_down = 1.0 - implied_up

    close_time = (market.get("close_time") or event.get("strike_date")
                  or event.get("close_time", ""))
    window_close_ts = _iso_to_ts(close_time) if close_time else 0

    # Log horizon mismatch if Kalshi window ≠ requested horizon
    kalshi_horizon = _infer_horizon(event)
    if kalshi_horizon and kalshi_horizon != horizon:
        logger.info(
            "Kalshi horizon mismatch: requested %s but nearest available is %s "
            "(asset=%s). This is expected — used for cross-venue arb in Phase 5.",
            horizon, kalshi_horizon, asset,
        )

    return MarketData(
        asset=asset,
        venue="kalshi",
        horizon=kalshi_horizon or horizon,
        up_ref=market_ticker,
        down_ref=market_ticker,    # binary: down = 1 − implied_up
        implied_up=implied_up,
        implied_down=implied_down,
        window_close_ts=window_close_ts,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _infer_horizon(event: dict) -> str | None:
    """Attempt to infer the window horizon from Kalshi event metadata."""
    title = (event.get("sub_title") or event.get("title") or "").lower()
    if "15" in title:
        return "15m"
    if "hour" in title or "60" in title:
        return "1h"
    if "5" in title:
        return "5m"
    return None
