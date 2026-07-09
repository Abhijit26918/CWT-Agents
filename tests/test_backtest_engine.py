"""Tests for core/backtest/engine.py — walk-forward replay, no network, no real Kronos.

Uses the same FakePredictor convention as tests/test_pipeline_fakes.py: it
always predicts last_close * 1.01 (always_up=True), so p_up is deterministically
1.0 for every window regardless of window content. That lets the expected
Brier score, hit rate, and synthetic PnL be computed by hand and asserted exactly.
"""
import pandas as pd
import pytest

from core.backtest.engine import run_backtest
from core.config import load_config


class FakePredictor:
    def __init__(self, always_up=True):
        self.always_up = always_up

    def predict(self, df, x_timestamp, y_timestamp, pred_len=1,
                T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False):
        last_close = float(df["close"].iloc[-1])
        predicted = last_close * (1.01 if self.always_up else 0.99)
        return pd.DataFrame(
            {"open": [predicted], "high": [predicted], "low": [predicted],
             "close": [predicted], "volume": [0.0], "amount": [0.0]},
            index=y_timestamp,
        )


@pytest.fixture
def small_cfg():
    cfg = load_config("config.yaml")
    small_kronos = cfg.kronos.model_copy(update={"lookback": 5, "mc_samples": 3})
    return cfg.model_copy(update={"kronos": small_kronos})


def _synthetic_df(n_context: int, n_up: int, n_down: int) -> pd.DataFrame:
    """n_context filler bars, then n_up bars closing UP, then n_down bars closing DOWN."""
    rows = []
    t = 1_700_000_000
    step = 300
    for _ in range(n_context):
        rows.append({"open_time": t, "open": 100.0, "high": 100.0, "low": 100.0,
                      "close": 100.0, "volume": 1.0, "amount": 100.0})
        t += step
    for _ in range(n_up):
        rows.append({"open_time": t, "open": 100.0, "high": 101.0, "low": 99.0,
                      "close": 101.0, "volume": 1.0, "amount": 100.0})
        t += step
    for _ in range(n_down):
        rows.append({"open_time": t, "open": 100.0, "high": 101.0, "low": 99.0,
                      "close": 99.0, "volume": 1.0, "amount": 100.0})
        t += step
    return pd.DataFrame(rows)


def test_run_backtest_brier_and_hit_rate_all_windows(small_cfg):
    # lookback=5 context bars, then 15 UP-closing bars, then 10 DOWN-closing bars.
    df = _synthetic_df(n_context=5, n_up=15, n_down=10)

    result = run_backtest(
        df, small_cfg, "BTC",
        predictor=FakePredictor(always_up=True),
        market_p_up=0.50, stride=1, max_windows=None,
    )
    s = result.summary

    assert s.n_windows == 25
    assert s.n_traded == 25
    assert s.n_no_trade == 0
    assert s.brier_all == pytest.approx(10 / 25)   # (1-1)^2 for UP, (1-0)^2 for DOWN
    assert s.hit_rate_all == pytest.approx(15 / 25)
    assert s.brier_traded == pytest.approx(s.brier_all)
    assert s.hit_rate_traded == pytest.approx(s.hit_rate_all)


def test_run_backtest_synthetic_pnl_and_drawdown(small_cfg):
    df = _synthetic_df(n_context=5, n_up=15, n_down=10)

    result = run_backtest(
        df, small_cfg, "BTC",
        predictor=FakePredictor(always_up=True),
        market_p_up=0.50, stride=1, max_windows=None,
    )
    s = result.summary

    # Every trade stakes f_max=0.10 * bankroll=1000 = $100 (p_up=1.0 is constant,
    # so Kelly's raw fraction is constant and gets clamped to f_max every time).
    assert all(t.stake_paper == pytest.approx(100.0) for t in result.trades)
    # 15 wins @ +$100, 10 losses @ -$100 (c=market_p_up=0.5 → win pays 1:1).
    assert s.total_pnl_synthetic == pytest.approx(500.0)
    # Cumulative PnL climbs to $1500 after the 15 wins, then drops to $500 after
    # the 10 consecutive losses — peak-to-trough drawdown is $1000.
    assert s.max_drawdown_synthetic == pytest.approx(1000.0)


def test_run_backtest_no_trade_when_model_has_no_edge(small_cfg):
    df = _synthetic_df(n_context=5, n_up=10, n_down=10)

    # FakePredictor(always_up=False) → p_up ≈ 0.0. Market at 0.99 up / 0.01 down:
    # edge_up = 0 - 0.99 - fee < 0; edge_down = 1.0 - 0.01 - fee ≈ 0.98 > 0 → still trades DOWN.
    # Use market_p_up=0.50 with a predictor near 0.5 instead to force NONE — simplest is
    # to check that a real no-edge case (market matches model) yields NONE for every window.
    result = run_backtest(
        df, small_cfg, "BTC",
        predictor=FakePredictor(always_up=True),
        market_p_up=0.999,  # market already prices UP near-certain → no edge left after fee
        stride=1, max_windows=None,
    )
    sides = {t.side for t in result.trades}
    assert sides == {"NONE"}
    assert result.summary.n_traded == 0
    assert result.summary.total_pnl_synthetic == 0.0
    assert result.summary.brier_all is not None  # still computed even with no trades


def test_run_backtest_stride_and_max_windows_cap(small_cfg):
    df = _synthetic_df(n_context=5, n_up=15, n_down=10)

    result = run_backtest(
        df, small_cfg, "BTC",
        predictor=FakePredictor(always_up=True),
        stride=2, max_windows=5,
    )
    assert result.summary.n_windows <= 5


def test_run_backtest_raises_on_insufficient_history(small_cfg):
    df = _synthetic_df(n_context=5, n_up=0, n_down=0)  # only 5 bars, lookback=5 needs 7+
    with pytest.raises(ValueError):
        run_backtest(df, small_cfg, "BTC", predictor=FakePredictor())
