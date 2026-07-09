"""Tests for core/data/binance_klines.py — pagination, normalization, persistence.

Mirrors tests/test_kalshi.py's convention: mock real HTTP via `responses`,
no network, no Apify/Binance credentials needed.
"""
import time

import responses

from core.data.binance_klines import (
    BINANCE_BASE,
    fetch_and_store_history,
    fetch_historical_klines,
    fetch_ohlcv_live,
)
from core.db import init_db

_STEP_MS = 300_000  # 5m


def _row(open_time_ms: int, close: float) -> list:
    return [open_time_ms, close - 1, close + 1, close - 2, close, 10.0,
            open_time_ms + _STEP_MS - 1, 100.0, 5, 4.0, 40.0, "0"]


@responses.activate
def test_fetch_historical_klines_single_page():
    rows = [_row(1_000_000_000_000 + i * _STEP_MS, 100 + i) for i in range(3)]
    responses.add(responses.GET, BINANCE_BASE, json=rows, status=200)

    result = fetch_historical_klines("BTCUSDT", interval="5m", days=1, page_limit=10)

    assert result == rows


@responses.activate
def test_fetch_historical_klines_paginates_until_short_page():
    page1 = [_row(1_000_000_000_000 + i * _STEP_MS, 100 + i) for i in range(3)]
    page2 = [_row(1_000_000_000_000 + (3 + i) * _STEP_MS, 200 + i) for i in range(2)]
    responses.add(responses.GET, BINANCE_BASE, json=page1, status=200)
    responses.add(responses.GET, BINANCE_BASE, json=page2, status=200)

    result = fetch_historical_klines("BTCUSDT", interval="5m", days=1, page_limit=3)

    assert len(result) == 5
    assert [r[0] for r in result] == [row[0] for row in page1 + page2]


@responses.activate
def test_fetch_historical_klines_excludes_still_forming_candle():
    """Regression: Binance includes the currently-forming candle if its
    open_time is in range. Using it as 'the last closed bar' leaks a live/
    partial price into the model's reference point — see core/data/binance_klines.py
    fetch_historical_klines docstring and the live-loop bug it caused."""
    now_ms = int(time.time() * 1000)
    closed_open_ms = (now_ms // _STEP_MS) * _STEP_MS - _STEP_MS  # previous full boundary
    forming_open_ms = closed_open_ms + _STEP_MS                   # currently in progress
    rows = [_row(closed_open_ms, 100.0), _row(forming_open_ms, 101.0)]
    responses.add(responses.GET, BINANCE_BASE, json=rows, status=200)

    result = fetch_historical_klines("BTCUSDT", interval="5m", days=1, page_limit=10)

    assert [r[0] for r in result] == [closed_open_ms]


@responses.activate
def test_fetch_historical_klines_empty_page_stops():
    responses.add(responses.GET, BINANCE_BASE, json=[], status=200)

    result = fetch_historical_klines("BTCUSDT", interval="5m", days=1, page_limit=10)

    assert result == []


@responses.activate
def test_fetch_and_store_history_normalizes_and_persists(tmp_path):
    rows = [_row(1_000_000_000_000 + i * _STEP_MS, 100 + i) for i in range(5)]
    responses.add(responses.GET, BINANCE_BASE, json=rows, status=200)

    conn = init_db(tmp_path / "backtest.db")
    df = fetch_and_store_history(
        asset="BTC", symbol="BTCUSDT", interval="5m", days=1, conn=conn,
    )

    assert len(df) == 5
    assert list(df.columns) == ["open_time", "open", "high", "low", "close", "volume", "amount"]
    assert df["open_time"].is_monotonic_increasing

    persisted = conn.execute(
        "SELECT COUNT(*), MIN(source) FROM ohlcv WHERE asset = 'BTC' AND interval = '5m'"
    ).fetchone()
    assert persisted[0] == 5
    assert persisted[1] == "binance_direct"
    conn.close()


@responses.activate
def test_fetch_and_store_history_empty_returns_empty_df():
    responses.add(responses.GET, BINANCE_BASE, json=[], status=200)

    df = fetch_and_store_history(asset="BTC", symbol="BTCUSDT", interval="5m", days=1)

    assert df.empty


@responses.activate
def test_fetch_ohlcv_live_matches_apify_fetch_ohlcv_call_signature(tmp_path):
    """run_flow.py passes this as pipeline.run_once(ohlcv_fetcher=...) using the
    exact same kwargs it passes to apify_ohlcv.fetch_ohlcv — verify it accepts
    (and ignores) the Apify-only kwargs (client, use_cache, actor_id) without error."""
    rows = [_row(1_000_000_000_000 + i * _STEP_MS, 100 + i) for i in range(10)]
    responses.add(responses.GET, BINANCE_BASE, json=rows, status=200)

    conn = init_db(tmp_path / "cwt.db")
    df = fetch_ohlcv_live(
        asset="BTC", symbol="BTCUSDT", interval="5m", limit=5,
        conn=conn, client=object(), use_cache=True, actor_id="unused",
    )

    assert len(df) == 5   # tail(limit) applied
    persisted = conn.execute(
        "SELECT COUNT(*), MIN(source) FROM ohlcv WHERE asset='BTC'"
    ).fetchone()
    assert persisted[0] == 10   # all fetched rows persisted, not just the tail
    assert persisted[1] == "binance_direct"
    conn.close()
