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
        n = len(y_timestamp)
        return pd.DataFrame(
            {
                "open": [predicted] * n, "high": [predicted] * n,
                "low": [predicted] * n, "close": [predicted] * n,
                "volume": [0.0] * n, "amount": [0.0] * n,
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
# pred_len (multi-step-ahead forecasting — used by the delayed-confirmation flow)
# ---------------------------------------------------------------------------

class _RecordingPredictor:
    """Records the y_timestamp it was called with, and returns a predicted
    close that only goes UP on the LAST step (down on every earlier step) —
    disambiguates "uses the last forecast step" from "uses the first"."""
    def __init__(self):
        self.last_call_y_timestamp = None

    def predict(self, df, x_timestamp, y_timestamp, pred_len=1,
                T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False):
        self.last_call_y_timestamp = y_timestamp
        last_close = float(df["close"].iloc[-1])
        n = len(y_timestamp)
        closes = [last_close * 0.99] * (n - 1) + [last_close * 1.01]  # down,...,down,UP
        return pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes,
             "volume": [0.0] * n, "amount": [0.0] * n},
            index=y_timestamp,
        )


def test_predict_move_pred_len_builds_n_future_timestamps(ohlcv_df, cfg):
    recorder = _RecordingPredictor()
    predict_move(ohlcv_df, cfg, predictor=recorder, pred_len=5)

    assert len(recorder.last_call_y_timestamp) == 5
    steps = recorder.last_call_y_timestamp.diff().dropna().unique()
    assert len(steps) == 1  # evenly spaced by the interval

    interval_secs = 300  # config's 5m
    assert steps[0].total_seconds() == interval_secs


def test_predict_move_uses_last_step_not_first(ohlcv_df, cfg):
    """With pred_len=5, the up/down decision must use the 5th (last) forecast
    step's close, not the 1st — otherwise pred_len wouldn't mean anything."""
    p_up = predict_move(ohlcv_df, cfg, predictor=_RecordingPredictor(), pred_len=5)
    assert p_up == pytest.approx(1.0)  # last step is always UP by construction


def test_predict_move_default_pred_len_is_one(ohlcv_df, cfg):
    recorder = _RecordingPredictor()
    predict_move(ohlcv_df, cfg, predictor=recorder)
    assert len(recorder.last_call_y_timestamp) == 1


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
