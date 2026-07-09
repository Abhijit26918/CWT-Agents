"""Tests for the delayed-confirmation flow (run_once(confirm=True) +
confirm_candidates): candidate parked as PENDING_CONFIRM, then promoted to
OPEN or dropped as REJECTED by a second, faster-interval model check.

No network, no real Kronos — same FakePredictor/FakeApifyClient convention
as tests/test_pipeline_fakes.py.
"""
import json
import time
from pathlib import Path

import pandas as pd
import pytest

from core.config import load_config
from core.data.apify_ohlcv import normalize_ohlcv
from core.db import init_db
from core.markets import MarketData
from core.pipeline import confirm_candidates, run_once

FIXTURES = Path(__file__).parent / "fixtures"
RAW_ROWS = json.loads((FIXTURES / "binance_btc_5m.json").read_text())


class _FakeDataset:
    def __init__(self, rows): self._rows = rows
    def iterate_items(self): return iter(self._rows)

class _FakeActor:
    def call(self, run_input): return {"defaultDatasetId": "fake-ds"}

class FakeApifyClient:
    def actor(self, actor_id): return _FakeActor()
    def dataset(self, dataset_id): return _FakeDataset(RAW_ROWS)


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


def _fake_ohlcv_fetcher(asset, symbol, interval, limit, conn=None, client=None,
                         use_cache=False, actor_id=None):
    """Returns fixture data regardless of interval — good enough since
    FakePredictor only cares about the last close, not real bar timing."""
    return normalize_ohlcv(RAW_ROWS, symbol)


def _fake_market(asset: str, venue: str, p_up: float = 0.45):
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
# run_once(confirm=True) — candidate creation
# ---------------------------------------------------------------------------

def test_confirm_creates_pending_candidates_not_open(cfg, conn):
    report = run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.45),
        confirm=True,
    )
    assert len(report.rows) == 4
    assert {r.status for r in report.rows} == {"PENDING_CONFIRM"}

    statuses = {r[0] for r in conn.execute("SELECT DISTINCT status FROM predictions")}
    assert statuses == {"PENDING_CONFIRM"}


def test_confirm_skips_candidates_with_no_edge(cfg, conn):
    report = run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.999),  # model p_up~1.0, market already near-certain -> no edge after fee
        confirm=True,
    )
    assert report.rows == []
    count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    assert count == 0


def test_confirm_does_not_duplicate_candidate_while_one_is_active(cfg, conn):
    finders = _make_market_finders(p_up=0.45)
    run_once(cfg, conn, apify_client=FakeApifyClient(),
             predictor=FakePredictor(always_up=True), market_finders=finders, confirm=True)
    run_once(cfg, conn, apify_client=FakeApifyClient(),
             predictor=FakePredictor(always_up=True), market_finders=finders, confirm=True)

    count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    assert count == 4  # still just the first run's candidates, not 8


# ---------------------------------------------------------------------------
# confirm_candidates — promotion / rejection
# ---------------------------------------------------------------------------

def _create_due_candidate(cfg, conn, candidate_predictor, market_p_up=0.45):
    """Create one PENDING_CONFIRM candidate whose confirm_at_ts is already due."""
    run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=candidate_predictor,
        market_finders=_make_market_finders(p_up=market_p_up),
        venues=("polymarket",),
        confirm=True,
        confirm_delay_seconds=-1,  # already due by the time we check
    )


def test_confirm_candidates_promotes_agreeing_candidate_to_open(cfg, conn):
    _create_due_candidate(cfg, conn, FakePredictor(always_up=True), market_p_up=0.45)

    counts = confirm_candidates(
        conn, cfg,
        ohlcv_fetcher=_fake_ohlcv_fetcher,
        predictor=FakePredictor(always_up=True),   # 1m model also says UP -> agrees
        market_finders=_make_market_finders(p_up=0.45),
    )
    assert counts == {"confirmed": 2, "rejected": 0, "errors": 0}  # BTC + ETH

    row = conn.execute(
        "SELECT status, side, model_p_up_1m FROM predictions LIMIT 1"
    ).fetchone()
    assert row[0] == "OPEN"
    assert row[1] == "UP"
    assert row[2] is not None


def test_confirm_candidates_rejects_disagreeing_candidate(cfg, conn):
    _create_due_candidate(cfg, conn, FakePredictor(always_up=True), market_p_up=0.45)

    counts = confirm_candidates(
        conn, cfg,
        ohlcv_fetcher=_fake_ohlcv_fetcher,
        predictor=FakePredictor(always_up=False),  # 1m model says DOWN -> disagrees
        market_finders=_make_market_finders(p_up=0.45),
    )
    assert counts["rejected"] == 2
    assert counts["confirmed"] == 0

    statuses = {r[0] for r in conn.execute("SELECT DISTINCT status FROM predictions")}
    assert statuses == {"REJECTED"}


def test_confirm_candidates_rejects_when_market_moved_against_side(cfg, conn):
    _create_due_candidate(cfg, conn, FakePredictor(always_up=True), market_p_up=0.45)

    # 1m model agrees (still UP), but market re-price at confirm time is now
    # near-certain UP too, so the fee-adjusted edge is gone.
    counts = confirm_candidates(
        conn, cfg,
        ohlcv_fetcher=_fake_ohlcv_fetcher,
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.999),
    )
    assert counts["rejected"] == 2
    assert counts["confirmed"] == 0


def test_confirm_candidates_skips_not_yet_due(cfg, conn):
    run_once(
        cfg, conn,
        apify_client=FakeApifyClient(),
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.45),
        venues=("polymarket",),
        confirm=True,
        confirm_delay_seconds=3600,  # not due for an hour
    )

    counts = confirm_candidates(
        conn, cfg,
        ohlcv_fetcher=_fake_ohlcv_fetcher,
        predictor=FakePredictor(always_up=True),
        market_finders=_make_market_finders(p_up=0.45),
    )
    assert counts == {"confirmed": 0, "rejected": 0, "errors": 0}

    statuses = {r[0] for r in conn.execute("SELECT DISTINCT status FROM predictions")}
    assert statuses == {"PENDING_CONFIRM"}
