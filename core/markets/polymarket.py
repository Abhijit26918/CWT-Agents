"""Polymarket market discovery + implied probability. MASTER_PLAN.md §2 Agent 1.

Public read-only — no auth required.
Gamma API:  https://gamma-api.polymarket.com/events?slug={slug}
CLOB API:   https://clob.polymarket.com/midpoint?token_id={token_id}
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import requests

from core.markets import MarketData, MarketNotIndexed

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
TIMEOUT = 10

HORIZON_SECONDS: dict[str, int] = {"5m": 300, "15m": 900}


# ---------------------------------------------------------------------------
# Pure / deterministic helpers (unit-testable with no network)
# ---------------------------------------------------------------------------

def window_start(horizon: str, now_ts: int | None = None) -> int:
    """Return the UTC Unix timestamp of the current window's start."""
    secs = HORIZON_SECONDS[horizon]
    ts = now_ts if now_ts is not None else int(time.time())
    return (ts // secs) * secs


def make_slug(asset: str, horizon: str, ts: int) -> str:
    return f"{asset.lower()}-updown-{horizon}-{ts}"


def _iso_to_ts(iso_str: str) -> int:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(dt.timestamp())


def _parse_token_ids(raw: str | list) -> list[str]:
    """clobTokenIds arrives as a JSON string or already a list."""
    if isinstance(raw, list):
        return raw
    return json.loads(raw)


def _parse_outcomes(raw: str | list) -> list[str]:
    if isinstance(raw, list):
        return raw
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Network calls
# ---------------------------------------------------------------------------

def _fetch_event(slug: str) -> dict | None:
    url = f"{GAMMA_BASE}/events"
    resp = requests.get(url, params={"slug": slug}, timeout=TIMEOUT)
    resp.raise_for_status()
    events = resp.json()
    if not events:
        return None
    return events[0]


def _fetch_midpoint(token_id: str) -> float:
    url = f"{CLOB_BASE}/midpoint"
    resp = requests.get(url, params={"token_id": token_id}, timeout=TIMEOUT)
    resp.raise_for_status()
    return float(resp.json()["mid"])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_market(asset: str, event: dict, horizon: str) -> MarketData:
    market = event["markets"][0]
    outcomes = _parse_outcomes(market["outcomes"])
    token_ids = _parse_token_ids(market["clobTokenIds"])

    # Outcomes are ["Up", "Down"] — Up is always index 0
    up_idx = next(
        (i for i, o in enumerate(outcomes) if o.strip().lower() == "up"), 0
    )
    down_idx = 1 - up_idx

    up_token = token_ids[up_idx]
    down_token = token_ids[down_idx]

    end_date = market.get("endDateIso") or event.get("endDate", "")
    window_close_ts = _iso_to_ts(end_date) if end_date else 0

    implied_up = _fetch_midpoint(up_token)
    implied_down = _fetch_midpoint(down_token)

    return MarketData(
        asset=asset,
        venue="polymarket",
        horizon=horizon,
        up_ref=up_token,
        down_ref=down_token,
        implied_up=implied_up,
        implied_down=implied_down,
        window_close_ts=window_close_ts,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_market(
    asset: str,
    horizon: str = "5m",
    now_ts: int | None = None,
) -> MarketData:
    """Find the current window's up/down market for *asset* on Polymarket.

    If the slug for the current window is not indexed yet (common at boundaries),
    retries with the previous window's slug before raising MarketNotIndexed.
    """
    ws = window_start(horizon, now_ts)
    slug = make_slug(asset, horizon, ws)
    logger.debug("Polymarket: trying slug %s", slug)

    event = _fetch_event(slug)

    if event is None:
        prev_ws = ws - HORIZON_SECONDS[horizon]
        prev_slug = make_slug(asset, horizon, prev_ws)
        logger.warning(
            "Polymarket: slug %s not found, retrying previous window %s",
            slug,
            prev_slug,
        )
        event = _fetch_event(prev_slug)

    if event is None:
        raise MarketNotIndexed(
            f"No Polymarket event found for {asset} {horizon} "
            f"(tried {slug} and previous window)"
        )

    return _parse_market(asset, event, horizon)
