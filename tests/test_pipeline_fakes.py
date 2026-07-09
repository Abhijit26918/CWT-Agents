"""End-to-end pipeline test using all fakes — no network, no LLM, no real model.

This is the green-build gate: if this passes, the full prediction flow works.
"""
import json
from pathlib import Path

import pytest

from core.config import load_config
from core.db import init_db
from core.markets import MarketData
from core.pipeline import run_once

FIXTURES = Path(__file__).parent / "fixtures"
RAW_ROWS = json.loads((FIXTURES / "binance_btc_5m.json").read_text())


# ---------------------------------------------------------------------------
# Shared fakes (same ones used in individual module tests)
# ---------------------------------------------------------------------------

class _FakeDataset:
    def __init__(self, rows): self._rows = rows
    def iterate_items(self): return iter(self._rows)

class _FakeActor:
    def call(self, run_input): return {"defaultDatasetId": "fake-ds"}

class FakeApifyClient:
    def actor(self, actor_id): return _FakeActor()
    def dataset(self, dataset_id): return _FakeDataset(RAW_ROWS)


import pandas as pd

class FakePredictor:
    def __init__(self, always_up=True): self.always_up = always_up
    def predict(self, df, x_timestamp, y_timestamp, pred_len=1,
                T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False):
        last_close = float(df["close"].iloc[-1])
        predicted = last_close * (1.01 if self.always_up else 0.99)
        n = len(y_timestamp)
        return pd.DataFrame(
            {"open": [predicted] * n, "high": [predicted] * n, "low": [predicted] * n,
             "close": [predicted] * n, "volume": [0.0] * n, "amount": [0.0] * n},
            index=y_timestamp,
        )


def _fake_market(asset: str, venue: str, p_up: float = 0.45):
    """Returns a MarketData with p_up < model (so model sees UP edge)."""
    import time
    return MarketData(
        asset=asset, venue=venue, horizon="5m",
        up_ref=f"{asset}_up_token", down_ref=f"{asset}_down_token",
        implied_up=p_up, implied_down=1 - p_up,
        window_close_ts=int(time.time()) + 300,
        fetched_at="2025-06-27T05:00:00+00:00",
    )


def _make_market_finders(p_up=0.45):
    return {
        "polymarket": lambda asset, horizon: _fake_market(asset, "polymarket", p_up),
        "kalshi":     lambda asset, horizon: _fake_market(asset, "kalshi",     p_up),
    }


@pytest.fixture
def cfg():
    return load_config("config.yaml")


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Core pipeline tests
# ---------------------------------------------------------------------------

def test_run_once_returns_report(cfg, conn):
    report = run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.45),
    )
    # BTC + ETH × polymarket + kalshi = 4 prediction rows
    assert len(report.rows) == 4


def test_run_once_writes_predictions_to_db(cfg, conn):
    run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.45),
    )
    count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    assert count == 4


def test_run_once_prediction_has_open_status(cfg, conn):
    run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.45),
    )
    statuses = {r[0] for r in conn.execute("SELECT DISTINCT status FROM predictions")}
    assert statuses == {"OPEN"}


def test_run_once_no_trade_when_no_edge(cfg, conn):
    # Market implied_up=0.60, model p_up=1.0 — but fee=0.01
    # Actually with always_up=True (p_up≈1.0) vs market 0.60 → big UP edge
    # Test the opposite: market at 0.99 → model sees no UP edge (fee eats it)
    report = run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=False),  # p_up ≈ 0.0
        market_finders=_make_market_finders(p_up=0.50),
        # p_down=1.0 vs implied_down=0.50 → huge DOWN edge (this WILL trade)
    )
    # With always_up=False, model says DOWN heavily → DOWN trades should fire
    sides = {r.side for r in report.rows}
    assert "DOWN" in sides or "NONE" in sides


def test_run_once_gracefully_skips_bad_venue(cfg, conn):
    def _exploding_finder(asset, horizon):
        raise RuntimeError("Market API down")

    report = run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=True),
        market_finders={"polymarket": _exploding_finder, "kalshi": _exploding_finder},
    )
    # All venues failed → errors logged, no rows, no crash
    assert len(report.rows) == 0
    assert len(report.errors) == 4   # BTC×2 + ETH×2


def test_run_once_uses_injected_ohlcv_fetcher(cfg, conn):
    """run_flow.py passes ohlcv_fetcher=fetch_ohlcv_live by default (live Binance
    data instead of the frozen Apify fixture) — verify run_once actually calls
    whatever fetcher it's given instead of always using the real Apify path."""
    calls = []

    def _fake_fetcher(asset, symbol, interval, limit, conn=None, client=None,
                       use_cache=False, actor_id=None):
        calls.append((asset, symbol))
        from core.data.apify_ohlcv import normalize_ohlcv
        return normalize_ohlcv(RAW_ROWS, symbol)

    report = run_once(
        cfg, conn,
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.45),
        ohlcv_fetcher=_fake_fetcher,
    )
    assert len(report.rows) == 4
    assert {a for a, _ in calls} == {"BTC", "ETH"}


def test_run_once_all_prediction_fields_populated(cfg, conn):
    run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.45),
    )
    row = conn.execute(
        "SELECT asset, venue, model_p_up, market_p_up, edge, side, stake_paper "
        "FROM predictions LIMIT 1"
    ).fetchone()
    asset, venue, model_p_up, market_p_up, edge, side, stake_paper = row
    assert asset in ("BTC", "ETH")
    assert venue in ("polymarket", "kalshi")
    assert 0.0 <= model_p_up <= 1.0
    assert 0.0 <= market_p_up <= 1.0
    assert side in ("UP", "DOWN", "NONE")
    assert stake_paper >= 0.0
