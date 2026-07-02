"""Kronos K-line forecasting → Monte-Carlo P(up). MASTER_PLAN.md §2 Agent 3.

Model is loaded once as a module-level singleton (the slow part is loading,
not inference). Pass a custom `predictor` to predict_move() to bypass the
real model — used by unit tests via FakePredictor.

CPU SETUP NOTE:
  This project defaults to device="cpu". No CUDA required.
  Kronos-mini (4M params, config key kronos.model="NeoQuasar/Kronos-mini")
  is much faster on CPU than Kronos-small — switch in config.yaml for demos.
  HuggingFace weights cache to $HF_HOME (set in .env to keep on D drive).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Vendor path — added once at import time
_VENDOR = Path(__file__).parent.parent.parent / "vendor" / "Kronos"

_predictor_singleton: Any | None = None


# ---------------------------------------------------------------------------
# Singleton loader
# ---------------------------------------------------------------------------

def _load_predictor(model_id: str, tokenizer_id: str, device: str, max_context: int):
    """Load Kronos model + tokenizer and return a KronosPredictor."""
    if str(_VENDOR) not in sys.path:
        sys.path.insert(0, str(_VENDOR))

    from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: PLC0415

    logger.info("Loading Kronos tokenizer: %s", tokenizer_id)
    tok = KronosTokenizer.from_pretrained(tokenizer_id)

    logger.info("Loading Kronos model: %s (device=%s)", model_id, device)
    mdl = Kronos.from_pretrained(model_id)
    mdl.to(device)

    return KronosPredictor(mdl, tok, device=device, max_context=max_context)


def get_predictor(cfg) -> Any:
    global _predictor_singleton
    if _predictor_singleton is None:
        k = cfg.kronos
        _predictor_singleton = _load_predictor(k.model, k.tokenizer, k.device, k.lookback)
    return _predictor_singleton


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _build_timestamps(open_times_s: pd.Series, interval_seconds: int):
    """Return (x_timestamp, y_timestamp) as timezone-naive pandas Series.

    Kronos's calc_time_stamps expects a Series (uses .dt accessor), not a
    DatetimeIndex, so we pass pd.Series in both cases.
    """
    x_ts = pd.to_datetime(open_times_s.astype("int64"), unit="s")
    next_s = int(open_times_s.iloc[-1]) + interval_seconds
    y_ts = pd.to_datetime(pd.Series([next_s]), unit="s")
    return x_ts, y_ts


_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def predict_move(
    df: pd.DataFrame,
    cfg,
    predictor: Any | None = None,
) -> float:
    """Return P(up) for the next bar using Monte-Carlo sampling over Kronos.

    Args:
        df:        Full OHLCV DataFrame (up to 1000 rows).
        cfg:       AppConfig — uses cfg.kronos.lookback, mc_samples, etc.
        predictor: Injectable predictor for testing (FakePredictor). If None,
                   loads the real Kronos model via get_predictor(cfg).

    Returns:
        float in [0, 1] — fraction of MC samples predicting close > last_close.
    """
    k = cfg.kronos
    interval_secs = _INTERVAL_SECONDS.get(cfg.ohlcv_interval, 300)

    # Slice to Kronos context cap (≤512 for small/base, ≤2048 for mini)
    lookback = df.tail(k.lookback).copy()
    last_close = float(lookback["close"].iloc[-1])

    x_ts, y_ts = _build_timestamps(lookback["open_time"], interval_secs)

    if predictor is None:
        predictor = get_predictor(cfg)

    ups = 0
    for _ in range(k.mc_samples):
        pred = predictor.predict(
            df=lookback[["open", "high", "low", "close", "volume", "amount"]],
            x_timestamp=x_ts,
            y_timestamp=y_ts,
            pred_len=1,
            T=1.0,
            top_p=0.9,
            sample_count=1,
            verbose=False,
        )
        if float(pred["close"].iloc[-1]) > last_close:
            ups += 1

    p_up = ups / k.mc_samples
    logger.debug(
        "Kronos MC: %d/%d samples UP → p_up=%.3f (last_close=%.4f)",
        ups, k.mc_samples, p_up, last_close,
    )
    return p_up
