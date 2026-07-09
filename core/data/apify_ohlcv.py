"""Apify-sourced OHLCV fetch + normalize + persist. MASTER_PLAN.md §2 Agent 2.

Uses the Binance klines actor on Apify (free monthly credits).
Accepts an injectable `client` for testing — pass a FakeApifyClient that
returns fixture data without touching the network.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Protocol

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".cache")
CACHE_FILE_TPL = ".cache/ohlcv_{symbol}_{interval}.json"

REQUIRED_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "amount"]


# ---------------------------------------------------------------------------
# Injectable client protocol (duck-typed for testing)
# ---------------------------------------------------------------------------

class ApifyClientProtocol(Protocol):
    def actor(self, actor_id: str) -> "ActorProtocol": ...


class ActorProtocol(Protocol):
    def call(self, run_input: dict) -> dict: ...


class DatasetProtocol(Protocol):
    def iterate_items(self): ...


# ---------------------------------------------------------------------------
# Normalization (pure function, fully testable)
# ---------------------------------------------------------------------------

def normalize_ohlcv(rows: list[dict], symbol: str) -> pd.DataFrame:
    """Convert raw Apify klines rows to a clean DataFrame."""
    df = pd.DataFrame(rows)

    # Flexible column name mapping from Binance actor variants
    rename = {
        "openTime": "open_time",
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "Volume": "volume",
        "quoteAssetVolume": "quote_volume",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    # Ensure numeric types
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # open_time: accept ms or s epoch integers
    if "open_time" in df.columns:
        df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("Int64")
        # Convert ms → s if values look like milliseconds (> year 2100 in seconds)
        if df["open_time"].dropna().iloc[0] > 4_000_000_000:
            df["open_time"] = (df["open_time"] // 1000).astype("Int64")

    # Compute amount = close × volume if missing
    if "amount" not in df.columns:
        df["amount"] = df["close"] * df["volume"]

    df = df.sort_values("open_time").reset_index(drop=True)
    return df[REQUIRED_COLUMNS]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist(df: pd.DataFrame, asset: str, interval: str, conn: sqlite3.Connection) -> None:
    rows = df.to_dict("records")
    conn.executemany(
        """INSERT OR IGNORE INTO ohlcv
           (asset, interval, open_time, open, high, low, close, volume, amount, source)
           VALUES (:asset, :interval, :open_time, :open, :high, :low, :close,
                   :volume, :amount, :source)""",
        [{**r, "asset": asset, "interval": interval, "source": "apify"} for r in rows],
    )
    conn.commit()
    logger.info("Persisted %d OHLCV rows for %s %s", len(rows), asset, interval)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def _cache_path(symbol: str, interval: str) -> Path:
    return Path(CACHE_FILE_TPL.format(symbol=symbol, interval=interval))


def _write_cache(rows: list[dict], symbol: str, interval: str) -> None:
    path = _cache_path(symbol, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))


def _read_cache(symbol: str, interval: str) -> list[dict] | None:
    path = _cache_path(symbol, interval)
    if path.exists():
        return json.loads(path.read_text())
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_ohlcv(
    asset: str,
    symbol: str,
    interval: str = "5m",
    limit: int = 1000,
    actor_id: str = "parseforge/binance-prices-scraper",
    conn: sqlite3.Connection | None = None,
    client=None,
    use_cache: bool = False,
) -> pd.DataFrame:
    """Fetch OHLCV bars for *asset* via Apify, normalize, and optionally persist.

    Args:
        asset:      "BTC" or "ETH" (used for DB storage).
        symbol:     Binance symbol e.g. "BTCUSDT".
        interval:   Kline interval e.g. "5m".
        limit:      Number of bars to fetch (up to 1000).
        actor_id:   Apify actor to use.
        conn:       Open SQLite connection — rows persisted when provided.
        client:     Injectable ApifyClient (or fake). If None, creates a real client
                    using APIFY_TOKEN from the environment.
        use_cache:  If True, return cached rows from disk without calling Apify.
    """
    if use_cache:
        cached = _read_cache(symbol, interval)
        if cached:
            logger.info("Using cached OHLCV for %s %s (%d rows)", symbol, interval, len(cached))
            df = normalize_ohlcv(cached, symbol)
            if conn is not None:
                _persist(df, asset, interval, conn)
            return df

    if client is None:
        import os
        from apify_client import ApifyClient
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            raise ValueError("APIFY_TOKEN not set in environment")
        client = ApifyClient(token)

    logger.info("Calling Apify actor %s for %s %s limit=%d", actor_id, symbol, interval, limit)

    run_input = {"mode": "klines", "symbol": symbol, "interval": interval, "limit": limit}

    for attempt in range(2):
        try:
            run = client.actor(actor_id).call(run_input=run_input)
            rows = list(client.dataset(run["defaultDatasetId"]).iterate_items())
            break
        except Exception as exc:
            if attempt == 0:
                logger.warning("Apify call failed (%s), retrying in 5s…", exc)
                time.sleep(5)
            else:
                raise

    _write_cache(rows, symbol, interval)
    df = normalize_ohlcv(rows, symbol)

    if conn is not None:
        _persist(df, asset, interval, conn)

    return df
