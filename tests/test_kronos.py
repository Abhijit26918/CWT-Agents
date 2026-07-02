"""Tests for core/predict/kronos_model.py — MC P(up) logic via FakePredictor.

The real Kronos model is NOT loaded in these tests. A FakePredictor returns a
fixed directional result so we can test the Monte-Carlo counting logic cleanly.
The opt-in real-model test is gated behind RUN_KRONOS_TESTS=1.
"""
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from core.config import load_config
from core.data.apify_ohlcv import normalize_ohlcv
from core.predict.kronos_model import predict_move

FIXTURES = Path(__file__).parent / "fixtures"
RAW_ROWS = json.loads((FIXTURES / "binance_btc_5m.json").read_text())


# ---------------------------------------------------------------------------
# Fake predictor
# ---------------------------------------------------------------------------

class FakePredictor:
    """Returns a predicted close that is always above or always below last_close."""

    def __init__(self, always_up: bool = True):
        self.always_up = always_up

    def predict(self, df, x_timestamp, y_timestamp, pred_len=1,
                T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False):
        last_close = float(df["close"].iloc[-1])
        predicted = last_close * (1.01 if self.always_up else 0.99)
        return pd.DataFrame(
            {
                "open": [predicted], "high": [predicted],
                "low": [predicted], "close": [predicted],
                "volume": [0.0], "amount": [0.0],
            },
            index=y_timestamp,
        )


@pytest.fixture
def ohlcv_df():
    return normalize_ohlcv(RAW_ROWS, "BTCUSDT")


@pytest.fixture
def cfg():
    return load_config("config.yaml")


# ---------------------------------------------------------------------------
# MC P(up) logic
# ---------------------------------------------------------------------------

def test_predict_move_all_up(ohlcv_df, cfg):
    p_up = predict_move(ohlcv_df, cfg, predictor=FakePredictor(always_up=True))
    assert p_up == pytest.approx(1.0)


def test_predict_move_all_down(ohlcv_df, cfg):
    p_up = predict_move(ohlcv_df, cfg, predictor=FakePredictor(always_up=False))
    assert p_up == pytest.approx(0.0)


def test_predict_move_returns_float_in_range(ohlcv_df, cfg):
    p_up = predict_move(ohlcv_df, cfg, predictor=FakePredictor(always_up=True))
    assert 0.0 <= p_up <= 1.0


def test_predict_move_uses_lookback_slice(ohlcv_df, cfg):
    """predict_move must not crash even when df has fewer rows than lookback."""
    short_df = ohlcv_df.head(5)
    p_up = predict_move(short_df, cfg, predictor=FakePredictor(always_up=True))
    assert isinstance(p_up, float)


# ---------------------------------------------------------------------------
# Optional: real Kronos model (gated — set RUN_KRONOS_TESTS=1 to run)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("RUN_KRONOS_TESTS"),
    reason="Set RUN_KRONOS_TESTS=1 to run the real Kronos model",
)
def test_predict_move_real_kronos(ohlcv_df, cfg):
    p_up = predict_move(ohlcv_df, cfg)  # loads real model
    assert 0.0 <= p_up <= 1.0
