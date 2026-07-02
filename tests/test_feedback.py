"""Tests for core/feedback/scoring.py — resolve, Brier, calibration, Kelly update."""
from datetime import datetime, timezone

import pytest

from core.config import load_config
from core.db import init_db
from core.feedback.scoring import score_predictions, _update_calibration


@pytest.fixture
def cfg():
    return load_config("config.yaml")


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_ohlcv(conn, asset, open_time, open_price, close_price):
    conn.execute(
        "INSERT OR IGNORE INTO ohlcv (asset, interval, open_time, open, high, low, close, volume, amount, source) "
        "VALUES (?, '5m', ?, ?, ?, ?, ?, 10.0, 1000.0, 'test')",
        (asset, open_time, open_price, max(open_price, close_price),
         min(open_price, close_price), close_price),
    )
    conn.commit()


def _seed_prediction(conn, asset, venue, model_p_up, market_p_up, side, stake,
                     window_close_ts):
    conn.execute(
        """INSERT INTO predictions
           (ts, asset, venue, horizon, model_p_up, market_p_up, edge,
            side, kelly_fraction, stake_paper, window_close_ts, status, created_at)
           VALUES (?, ?, ?, '5m', ?, ?, 0.05, ?, 0.05, ?, ?, 'OPEN', ?)""",
        (
            window_close_ts - 300,
            asset, venue, model_p_up, market_p_up,
            side, stake, window_close_ts,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ---------------------------------------------------------------------------
# Resolution tests
# ---------------------------------------------------------------------------

def test_resolves_winning_up_prediction(conn, cfg):
    wc_ts = 1751000400
    _seed_ohlcv(conn, "BTC", wc_ts - 300, open_price=67000, close_price=67200)  # UP
    pred_id = _seed_prediction(conn, "BTC", "polymarket", 0.60, 0.50, "UP", 25.0, wc_ts)

    resolved = score_predictions(conn, cfg)

    assert resolved == 1
    outcome = conn.execute(
        "SELECT actual_direction, won, pnl_paper FROM outcomes WHERE prediction_id=?",
        (pred_id,)
    ).fetchone()
    assert outcome[0] == "UP"
    assert outcome[1] == 1        # won
    assert outcome[2] > 0         # positive PnL


def test_resolves_losing_up_prediction(conn, cfg):
    wc_ts = 1751000400
    _seed_ohlcv(conn, "BTC", wc_ts - 300, open_price=67200, close_price=67000)  # DOWN
    pred_id = _seed_prediction(conn, "BTC", "polymarket", 0.60, 0.50, "UP", 25.0, wc_ts)

    score_predictions(conn, cfg)

    outcome = conn.execute(
        "SELECT won, pnl_paper FROM outcomes WHERE prediction_id=?", (pred_id,)
    ).fetchone()
    assert outcome[0] == 0        # lost
    assert outcome[1] == -25.0    # lost stake


def test_resolves_winning_down_prediction(conn, cfg):
    wc_ts = 1751000400
    _seed_ohlcv(conn, "BTC", wc_ts - 300, open_price=67200, close_price=67000)  # DOWN
    pred_id = _seed_prediction(conn, "BTC", "polymarket", 0.35, 0.50, "DOWN", 20.0, wc_ts)

    score_predictions(conn, cfg)

    outcome = conn.execute(
        "SELECT actual_direction, won FROM outcomes WHERE prediction_id=?", (pred_id,)
    ).fetchone()
    assert outcome[0] == "DOWN"
    assert outcome[1] == 1


def test_sets_status_resolved(conn, cfg):
    wc_ts = 1751000400
    _seed_ohlcv(conn, "BTC", wc_ts - 300, 67000, 67200)
    _seed_prediction(conn, "BTC", "polymarket", 0.60, 0.50, "UP", 25.0, wc_ts)

    score_predictions(conn, cfg)

    status = conn.execute(
        "SELECT status FROM predictions WHERE window_close_ts=?", (wc_ts,)
    ).fetchone()[0]
    assert status == "RESOLVED"


def test_skips_prediction_without_ohlcv(conn, cfg):
    future_ts = 9_999_999_999  # far future — won't have OHLCV yet but ts < now check?
    # Actually use a past ts but don't seed OHLCV
    wc_ts = 1751000400
    _seed_prediction(conn, "BTC", "polymarket", 0.60, 0.50, "UP", 25.0, wc_ts)
    # No OHLCV seeded → should skip (returns 0 resolved)

    resolved = score_predictions(conn, cfg)
    assert resolved == 0


def test_does_not_resolve_future_predictions(conn, cfg):
    future_wc = int(datetime.now(timezone.utc).timestamp()) + 3600
    _seed_prediction(conn, "BTC", "polymarket", 0.60, 0.50, "UP", 25.0, future_wc)

    resolved = score_predictions(conn, cfg)
    assert resolved == 0


# ---------------------------------------------------------------------------
# Calibration / Kelly multiplier tests
# ---------------------------------------------------------------------------

def test_kelly_shrinks_on_poor_calibration(conn, cfg):
    # Seed 5 predictions: model says UP (0.9) but market always went DOWN
    # Brier ≈ (0.9-0)^2 = 0.81 per sample → well above the shrink threshold (0.30)
    for i in range(5):
        wc_ts = 1751000400 + i * 300
        _seed_ohlcv(conn, "BTC", wc_ts - 300, 67200, 67000)  # open > close → DOWN
        _seed_prediction(conn, "BTC", "polymarket", 0.9, 0.50, "UP", 25.0, wc_ts)

    # Run the full scoring flow: resolves predictions → writes outcomes → updates calibration
    resolved = score_predictions(conn, cfg)
    assert resolved == 5

    cal = conn.execute(
        "SELECT kelly_multiplier, brier FROM calibration WHERE asset='BTC'"
    ).fetchone()
    assert cal is not None
    assert cal[0] < cfg.risk.kelly_multiplier  # Kelly shrunk due to poor Brier


def test_calibration_row_written(conn, cfg):
    wc_ts = 1751000400
    _seed_ohlcv(conn, "BTC", wc_ts - 300, 67000, 67200)
    pred_id = _seed_prediction(conn, "BTC", "polymarket", 0.60, 0.50, "UP", 25.0, wc_ts)
    # Manually insert outcome so _update_calibration has data
    conn.execute(
        "INSERT INTO outcomes (prediction_id, resolved_at, actual_direction, won, pnl_paper) "
        "VALUES (?, ?, 'UP', 1, 25.0)",
        (pred_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()

    _update_calibration(conn, "BTC", cfg.risk.kelly_multiplier)

    row = conn.execute("SELECT n, brier, hit_rate FROM calibration WHERE asset='BTC'").fetchone()
    assert row is not None
    assert row[0] == 1
    assert 0.0 <= row[1] <= 1.0
    assert 0.0 <= row[2] <= 1.0
