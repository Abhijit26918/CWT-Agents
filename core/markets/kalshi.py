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
    return min(events, key=lambda e: e["close_time"])


def _find_up_market(event_ticker: str) -> dict:
    """Return the UP (YES = up) market for the given event."""
    data = _get("/markets", {"event_ticker": event_ticker})
    markets = data.get("markets", [])
    if not markets:
        raise MarketNotIndexed(
            f"No markets found for Kalshi event {event_ticker}"
        )
    # Prefer a market whose subtitle/title signals "up"; fall back to first market.
    up_market = next(
        (m for m in markets if "up" in m.get("subtitle", "").lower()),
        markets[0],
    )
    return up_market


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
    event_ticker = event["ticker"]
    market = _find_up_market(event_ticker)

    market_ticker = market["ticker"]
    yes_bid = int(market.get("yes_bid", 0))
    yes_ask = int(market.get("yes_ask", 100))
    implied_up = (yes_bid + yes_ask) / 200.0
    implied_down = 1.0 - implied_up

    close_time = market.get("close_time") or event.get("close_time", "")
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
