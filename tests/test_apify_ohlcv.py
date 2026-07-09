"""Tests for core/data/apify_ohlcv.py — normalization and injectable fake client."""
import json
from pathlib import Path

import pandas as pd
import pytest

from core.data.apify_ohlcv import REQUIRED_COLUMNS, fetch_ohlcv, normalize_ohlcv

FIXTURES = Path(__file__).parent / "fixtures"
RAW_ROWS = json.loads((FIXTURES / "binance_btc_5m.json").read_text())


# ---------------------------------------------------------------------------
# Fake Apify client (no network, no token needed)
# ---------------------------------------------------------------------------

class _FakeDataset:
    def __init__(self, rows): self._rows = rows
    def iterate_items(self): return iter(self._rows)


class _FakeActor:
    def __init__(self, rows): self._rows = rows
    def call(self, run_input): return {"defaultDatasetId": "fake-ds"}


class FakeApifyClient:
    def __init__(self, rows=None):
        self._rows = rows or RAW_ROWS
    def actor(self, actor_id): return _FakeActor(self._rows)
    def dataset(self, dataset_id): return _FakeDataset(self._rows)


# ---------------------------------------------------------------------------
# normalize_ohlcv (pure function)
# ---------------------------------------------------------------------------

def test_normalize_produces_required_columns():
    df = normalize_ohlcv(RAW_ROWS, "BTCUSDT")
    assert list(df.columns) == REQUIRED_COLUMNS


def test_normalize_converts_opentime_ms_to_seconds():
    df = normalize_ohlcv(RAW_ROWS, "BTCUSDT")
    # Raw fixture has openTime in milliseconds (~1.75e12); normalized should be in seconds
    assert df["open_time"].iloc[0] < 4_000_000_000


def test_normalize_computes_amount():
    df = normalize_ohlcv(RAW_ROWS, "BTCUSDT")
    row = df.iloc[0]
    assert abs(row["amount"] - row["close"] * row["volume"]) < 1e-6


def test_normalize_sorts_ascending():
    import random
    shuffled = RAW_ROWS.copy()
    random.shuffle(shuffled)
    df = normalize_ohlcv(shuffled, "BTCUSDT")
    assert df["open_time"].is_monotonic_increasing


def test_normalize_correct_row_count():
    df = normalize_ohlcv(RAW_ROWS, "BTCUSDT")
    assert len(df) == len(RAW_ROWS)


def test_normalize_numeric_types():
    df = normalize_ohlcv(RAW_ROWS, "BTCUSDT")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        assert pd.api.types.is_numeric_dtype(df[col]), f"{col} should be numeric"


# ---------------------------------------------------------------------------
# fetch_ohlcv with fake client
# ---------------------------------------------------------------------------

def test_fetch_ohlcv_returns_dataframe():
    df = fetch_ohlcv(
        asset="BTC", symbol="BTCUSDT", interval="5m",
        client=FakeApifyClient(),
    )
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == REQUIRED_COLUMNS


def test_fetch_ohlcv_persists_to_db(tmp_path):
    import sqlite3
    from core.db import init_db
    conn = init_db(tmp_path / "test.db")

    fetch_ohlcv(
        asset="BTC", symbol="BTCUSDT", interval="5m",
        client=FakeApifyClient(), conn=conn,
    )

    rows = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE asset='BTC'").fetchone()[0]
    assert rows == len(RAW_ROWS)
    conn.close()


def test_fetch_ohlcv_cache_path_also_persists_to_db(tmp_path, monkeypatch):
    """Regression: use_cache=True used to return early without persisting to
    conn, so score_predictions could never resolve any prediction whenever the
    pipeline ran in cache mode (which run_flow.py always does — see
    run_flow.py:67). No OHLCV in the DB meant the live 'rolling paper'
    calibration stayed empty forever."""
    import core.data.apify_ohlcv as apify_ohlcv
    from core.db import init_db

    monkeypatch.setattr(
        apify_ohlcv, "CACHE_FILE_TPL", str(tmp_path / "ohlcv_{symbol}_{interval}.json")
    )
    apify_ohlcv._write_cache(RAW_ROWS, "BTCUSDT", "5m")

    conn = init_db(tmp_path / "test.db")
    df = fetch_ohlcv(asset="BTC", symbol="BTCUSDT", interval="5m",
                      conn=conn, use_cache=True)

    assert len(df) == len(RAW_ROWS)
    rows = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE asset='BTC'").fetchone()[0]
    assert rows == len(RAW_ROWS)
    conn.close()


def test_fetch_ohlcv_dedup_on_second_call(tmp_path):
    import sqlite3
    from core.db import init_db
    conn = init_db(tmp_path / "test.db")

    fetch_ohlcv(asset="BTC", symbol="BTCUSDT", interval="5m",
                client=FakeApifyClient(), conn=conn)
    fetch_ohlcv(asset="BTC", symbol="BTCUSDT", interval="5m",
                client=FakeApifyClient(), conn=conn)

    rows = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE asset='BTC'").fetchone()[0]
    assert rows == len(RAW_ROWS)   # no duplicates
    conn.close()
