"""Direct Binance public REST klines fetch — backtesting AND live cycles.

Apify's actor (core/data/apify_ohlcv.py) has no pagination and its `--cache`
path in run_flow.py served a frozen fixture whose timestamps never line up
with real time. fetch_ohlcv_live() below is a drop-in replacement (same call
signature as apify_ohlcv.fetch_ohlcv) that always hits Binance's free, keyless
/api/v3/klines endpoint for the freshest bars — used as run_flow.py's default
OHLCV source so live predictions can actually resolve against real bars.

Rows are normalized to the same shape as core.data.apify_ohlcv.normalize_ohlcv
and persisted with source="binance_direct" so they're distinguishable from
Apify-sourced rows in the same `ohlcv` table.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time

import pandas as pd
import requests

from core.data.apify_ohlcv import normalize_ohlcv

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com/api/v3/klines"
TIMEOUT = 10
PAGE_LIMIT = 1000

_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def _to_kline_rows(raw: list[list]) -> list[dict]:
    """Convert Binance's array-of-arrays kline format to normalize_ohlcv's dict rows."""
    return [
        {"openTime": r[0], "Open": r[1], "High": r[2], "Low": r[3],
         "Close": r[4], "Volume": r[5]}
        for r in raw
    ]


def _persist(df: pd.DataFrame, asset: str, interval: str, conn: sqlite3.Connection) -> None:
    rows = df.to_dict("records")
    conn.executemany(
        """INSERT OR IGNORE INTO ohlcv
           (asset, interval, open_time, open, high, low, close, volume, amount, source)
           VALUES (:asset, :interval, :open_time, :open, :high, :low, :close,
                   :volume, :amount, :source)""",
        [{**r, "asset": asset, "interval": interval, "source": "binance_direct"} for r in rows],
    )
    conn.commit()
    logger.info("Persisted %d historical OHLCV rows for %s %s", len(rows), asset, interval)


def fetch_historical_klines(
    symbol: str,
    interval: str = "5m",
    days: int = 60,
    session: requests.Session | None = None,
    page_limit: int = PAGE_LIMIT,
) -> list[list]:
    """Page through Binance's public klines endpoint for the last *days*.

    Returns raw kline rows (Binance's array-of-arrays format) across all pages,
    oldest first, EXCLUDING the currently-forming candle (Binance includes it
    in the response if its open_time falls in range, but its close price is
    still live/partial — treating it as "the last closed bar" would leak a
    mid-candle price into the model's reference point and effectively make it
    predict the same candle it was just shown. See core/backtest/engine.py
    and the live-loop bug this fixed for the full story).

    No API key required — this is the public market-data endpoint.

    `page_limit` defaults to Binance's max (1000) but is overridable so tests
    can exercise the pagination loop without 1000-row fixtures.
    """
    step_ms = _INTERVAL_MS.get(interval, 300_000)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000

    http = session or requests
    all_rows: list[list] = []
    cursor = start_ms

    while cursor < now_ms:
        resp = http.get(
            BINANCE_BASE,
            params={
                "symbol": symbol, "interval": interval,
                "startTime": cursor, "endTime": now_ms, "limit": page_limit,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        all_rows.extend(page)
        last_open_ms = page[-1][0]
        if len(page) < page_limit or last_open_ms + step_ms >= now_ms:
            break
        cursor = last_open_ms + step_ms

    return [r for r in all_rows if r[0] + step_ms <= now_ms]


def fetch_and_store_history(
    asset: str,
    symbol: str,
    interval: str = "5m",
    days: int = 60,
    conn: sqlite3.Connection | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Fetch *days* of historical klines for *symbol*, normalize, and optionally persist.

    Returns the normalized DataFrame (open_time, open, high, low, close, volume, amount).
    """
    raw = fetch_historical_klines(symbol, interval=interval, days=days, session=session)
    if not raw:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume", "amount"])

    df = normalize_ohlcv(_to_kline_rows(raw), symbol)
    df = df.drop_duplicates(subset="open_time").reset_index(drop=True)

    if conn is not None:
        _persist(df, asset, interval, conn)

    logger.info("Fetched %d historical bars for %s %s (%d days)", len(df), symbol, interval, days)
    return df


def fetch_ohlcv_live(
    asset: str,
    symbol: str,
    interval: str = "5m",
    limit: int = 1000,
    conn: sqlite3.Connection | None = None,
    client=None,               # unused; kept for call-signature parity with apify_ohlcv.fetch_ohlcv
    use_cache: bool = False,   # unused; this fetcher is always live
    actor_id: str | None = None,  # unused
) -> pd.DataFrame:
    """Drop-in OHLCV fetcher backed by Binance direct REST instead of Apify.

    Matches core.data.apify_ohlcv.fetch_ohlcv's call signature so it can be
    passed as pipeline.run_once(ohlcv_fetcher=fetch_ohlcv_live) — run_flow.py's
    default path, so each cycle resolves against real, currently-closing bars.
    """
    step_secs = _INTERVAL_MS.get(interval, 300_000) / 1000
    days = max(1, math.ceil(limit * step_secs / 86_400) + 1)
    df = fetch_and_store_history(asset, symbol, interval=interval, days=days, conn=conn)
    return df.tail(limit).reset_index(drop=True)
