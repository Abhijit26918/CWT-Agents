"""Walk-forward historical backtest.

Reuses the exact same prediction (core.predict.kronos_model.predict_move) and
sizing (core.risk.kelly.size_position) logic the live pipeline uses — the only
difference is the loop walks over already-known historical bars instead of
waiting for real time to pass, so every window's outcome is immediately known.

IMPORTANT LIMITATION (see files/EXPLANATION.md or the reviewer reply): there is
no historical archive of Polymarket/Kalshi implied odds, so `market_p_up` here
is a fixed synthetic value (default 0.50 = no-edge baseline), not a replay of
real market prices. Brier score and hit rate (which only need the model's
P(up) vs. the actual historical outcome) are therefore the metrics comparable
to the live "rolling paper" results. PnL/Kelly numbers are illustrative only —
labeled `_synthetic` throughout — since they're priced against a synthetic
market, not a real order book.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.config import AppConfig
from core.feedback.scoring import pnl_for_outcome
from core.predict.kronos_model import predict_move
from core.risk.kelly import size_position

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    window_open_time: int      # open_time of the last lookback bar
    resolved_open_time: int    # open_time of the bar whose outcome we checked
    model_p_up: float
    market_p_up: float
    side: str                  # UP | DOWN | NONE
    stake_paper: float
    actual_direction: str      # UP | DOWN
    won: bool
    pnl_paper: float


@dataclass
class BacktestSummary:
    asset: str
    n_windows: int
    n_traded: int
    n_no_trade: int
    start_open_time: int
    end_open_time: int
    # Calibration over ALL windows (model's implied direction vs actual) —
    # extra insight only possible because backtest has far more samples than
    # live paper trading; not filtered by whether a trade fired.
    brier_all: float
    hit_rate_all: float
    # Calibration over traded windows only (side != NONE) — this is the
    # subset that's methodologically comparable to the live calibration
    # table (core.feedback.scoring filters the same way).
    brier_traded: float | None
    hit_rate_traded: float | None
    # Synthetic PnL (see module docstring — priced against a fixed market_p_up,
    # not real historical market odds).
    total_pnl_synthetic: float
    mean_pnl_per_trade_synthetic: float | None
    pnl_std_synthetic: float | None
    sharpe_like_synthetic: float | None  # per-trade, NOT annualized
    max_drawdown_synthetic: float


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    summary: BacktestSummary | None = None

    def to_dict(self) -> dict:
        return {
            "summary": asdict(self.summary) if self.summary else None,
            "trades": [asdict(t) for t in self.trades],
        }


def _select_indices(lookback: int, n: int, stride: int, max_windows: int | None) -> list[int]:
    """Indices of the last bar of each lookback window (needs one more bar after it to resolve)."""
    all_idx = list(range(lookback - 1, n - 1, stride))
    if max_windows is not None and len(all_idx) > max_windows:
        picks = np.linspace(0, len(all_idx) - 1, max_windows).round().astype(int)
        all_idx = sorted({all_idx[p] for p in picks})
    return all_idx


def _seed_rngs(seed: int) -> None:
    """Best-effort determinism for Kronos's stochastic MC sampling (unseeded upstream)."""
    np.random.seed(seed)
    try:
        import torch
        torch.manual_seed(seed)
    except ImportError:
        pass


def run_backtest(
    df: pd.DataFrame,
    cfg: AppConfig,
    asset: str,
    predictor: Any | None = None,
    market_p_up: float = 0.50,
    stride: int = 1,
    max_windows: int | None = 500,
    seed: int | None = None,
) -> BacktestResult:
    """Replay predict_move + size_position over historical bars in *df*.

    Args:
        df:           Historical OHLCV, chronologically sorted (open_time ascending),
                       with columns open_time, open, high, low, close, volume, amount.
        cfg:          AppConfig — reuses cfg.kronos.lookback/mc_samples and cfg.risk.*.
        asset:        Asset label, for the summary only.
        predictor:    Injectable Kronos predictor (FakePredictor in tests; real
                      Kronos loaded lazily via predict_move if None).
        market_p_up:  Fixed synthetic market-implied P(up) — see module docstring.
        stride:       Only evaluate every Nth bar (keeps runtime bounded on CPU).
        max_windows:  Hard cap on windows evaluated, evenly subsampled across
                      the whole history if stride alone leaves too many. None = no cap.
        seed:         Best-effort RNG seed for reproducibility across runs.
    """
    lookback = cfg.kronos.lookback
    n = len(df)
    if n < lookback + 2:
        raise ValueError(
            f"Need at least {lookback + 2} historical bars for asset={asset}, "
            f"got {n}. Fetch more history first."
        )

    if seed is not None:
        _seed_rngs(seed)

    indices = _select_indices(lookback, n, stride, max_windows)
    logger.info(
        "Backtesting %s: %d windows (of %d bars, lookback=%d, stride=%d)",
        asset, len(indices), n, lookback, stride,
    )

    trades: list[BacktestTrade] = []
    for i in indices:
        window = df.iloc[i - lookback + 1: i + 1]
        p_up = predict_move(window, cfg, predictor=predictor)

        next_bar = df.iloc[i + 1]
        actual_direction = "UP" if next_bar["close"] > next_bar["open"] else "DOWN"

        decision = size_position(
            p_up=p_up,
            implied_up=market_p_up,
            implied_down=1.0 - market_p_up,
            kelly_multiplier=cfg.risk.kelly_multiplier,
            f_max=cfg.risk.f_max,
            fee=cfg.risk.fee,
            bankroll=cfg.risk.bankroll_paper,
        )
        won = decision.side == actual_direction
        pnl = pnl_for_outcome(decision.side, market_p_up, decision.stake_paper, won)

        trades.append(BacktestTrade(
            window_open_time=int(window["open_time"].iloc[-1]),
            resolved_open_time=int(next_bar["open_time"]),
            model_p_up=round(p_up, 6),
            market_p_up=market_p_up,
            side=decision.side,
            stake_paper=decision.stake_paper,
            actual_direction=actual_direction,
            won=won,
            pnl_paper=round(pnl, 4),
        ))

    summary = _summarize(asset, trades)
    return BacktestResult(trades=trades, summary=summary)


def _summarize(asset: str, trades: list[BacktestTrade]) -> BacktestSummary:
    n_windows = len(trades)
    traded = [t for t in trades if t.side != "NONE"]
    n_traded = len(traded)

    brier_all = sum(
        (t.model_p_up - (1.0 if t.actual_direction == "UP" else 0.0)) ** 2 for t in trades
    ) / n_windows
    model_dir_hits = sum(
        1 for t in trades
        if ("UP" if t.model_p_up >= 0.5 else "DOWN") == t.actual_direction
    )
    hit_rate_all = model_dir_hits / n_windows

    brier_traded = hit_rate_traded = None
    total_pnl = 0.0
    mean_pnl = pnl_std = sharpe = None
    max_dd = 0.0

    if traded:
        brier_traded = sum(
            (t.model_p_up - (1.0 if t.actual_direction == "UP" else 0.0)) ** 2 for t in traded
        ) / n_traded
        hit_rate_traded = sum(1 for t in traded if t.won) / n_traded

        pnls = np.array([t.pnl_paper for t in traded])
        total_pnl = float(pnls.sum())
        mean_pnl = float(pnls.mean())
        pnl_std = float(pnls.std()) if n_traded > 1 else 0.0
        sharpe = (mean_pnl / pnl_std * np.sqrt(n_traded)) if pnl_std > 0 else None

        cum = np.cumsum(pnls)
        running_max = np.maximum.accumulate(cum)
        max_dd = float((running_max - cum).max())

    return BacktestSummary(
        asset=asset,
        n_windows=n_windows,
        n_traded=n_traded,
        n_no_trade=n_windows - n_traded,
        start_open_time=trades[0].window_open_time,
        end_open_time=trades[-1].window_open_time,
        brier_all=round(brier_all, 6),
        hit_rate_all=round(hit_rate_all, 4),
        brier_traded=round(brier_traded, 6) if brier_traded is not None else None,
        hit_rate_traded=round(hit_rate_traded, 4) if hit_rate_traded is not None else None,
        total_pnl_synthetic=round(total_pnl, 2),
        mean_pnl_per_trade_synthetic=round(mean_pnl, 4) if mean_pnl is not None else None,
        pnl_std_synthetic=round(pnl_std, 4) if pnl_std is not None else None,
        sharpe_like_synthetic=round(sharpe, 4) if sharpe is not None else None,
        max_drawdown_synthetic=round(max_dd, 2),
    )
