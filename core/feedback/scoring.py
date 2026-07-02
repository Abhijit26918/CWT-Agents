"""Resolve matured predictions, score, recalibrate Kelly. MASTER_PLAN.md §2 Agent 5.

This is the Hermes "agent loop feedback" — it reads the outcomes of past bets
and adjusts the parameters the next decision will use. Runs at the top of
every pipeline cycle (score first, then predict).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_BRIER_SHRINK_THRESHOLD = 0.30   # poor calibration → shrink Kelly
_BRIER_RECOVER_THRESHOLD = 0.22  # well-calibrated → allow recovery
_HIT_RATE_MIN = 0.50             # also need > coin-flip hit-rate to recover
_KELLY_FLOOR = 0.05
_SHRINK_FACTOR = 0.80
_RECOVER_FACTOR = 1.10


# ---------------------------------------------------------------------------
# Actual direction lookup from cached OHLCV
# ---------------------------------------------------------------------------

def _get_actual_direction(
    conn: sqlite3.Connection,
    asset: str,
    window_close_ts: int,
    interval_seconds: int = 300,
) -> str | None:
    """Return "UP" or "DOWN" for the bar that closed at window_close_ts.

    Uses the OHLCV row where open_time = window_close_ts - interval_seconds,
    comparing that bar's open to its close.  Returns None if data is missing.
    """
    bar_open_time = window_close_ts - interval_seconds
    row = conn.execute(
        "SELECT open, close FROM ohlcv WHERE asset = ? AND open_time = ?",
        (asset, bar_open_time),
    ).fetchone()
    if row is None:
        logger.debug(
            "No OHLCV bar for %s at open_time=%d — outcome cannot be resolved yet",
            asset, bar_open_time,
        )
        return None
    open_price, close_price = row
    return "UP" if close_price > open_price else "DOWN"


# ---------------------------------------------------------------------------
# Calibration update
# ---------------------------------------------------------------------------

def _update_calibration(
    conn: sqlite3.Connection,
    asset: str,
    default_kelly: float,
) -> None:
    rows = conn.execute(
        """SELECT p.model_p_up, o.actual_direction, o.won
           FROM predictions p
           JOIN outcomes o ON p.id = o.prediction_id
           WHERE p.asset = ? AND p.side != 'NONE'""",
        (asset,),
    ).fetchall()

    if not rows:
        return

    n = len(rows)
    brier = sum(
        (p_up - (1.0 if direction == "UP" else 0.0)) ** 2
        for p_up, direction, _ in rows
    ) / n
    hit_rate = sum(won for _, _, won in rows) / n

    # Current multiplier (from calibration table or config default)
    current = conn.execute(
        "SELECT kelly_multiplier FROM calibration WHERE asset = ?", (asset,)
    ).fetchone()
    kelly_mult = current[0] if current else default_kelly

    if brier > _BRIER_SHRINK_THRESHOLD:
        new_mult = max(kelly_mult * _SHRINK_FACTOR, _KELLY_FLOOR)
        logger.info(
            "Calibration %s: Brier=%.3f > %.2f → shrinking Kelly %.3f → %.3f",
            asset, brier, _BRIER_SHRINK_THRESHOLD, kelly_mult, new_mult,
        )
    elif brier < _BRIER_RECOVER_THRESHOLD and hit_rate > _HIT_RATE_MIN:
        new_mult = min(kelly_mult * _RECOVER_FACTOR, default_kelly)
        logger.info(
            "Calibration %s: Brier=%.3f, hit_rate=%.2f → recovering Kelly %.3f → %.3f",
            asset, brier, hit_rate, kelly_mult, new_mult,
        )
    else:
        new_mult = kelly_mult

    conn.execute(
        """INSERT INTO calibration (asset, n, brier, hit_rate, kelly_multiplier, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(asset) DO UPDATE SET
             n=excluded.n, brier=excluded.brier, hit_rate=excluded.hit_rate,
             kelly_multiplier=excluded.kelly_multiplier, updated_at=excluded.updated_at""",
        (asset, n, round(brier, 6), round(hit_rate, 4), round(new_mult, 6),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def score_predictions(conn: sqlite3.Connection, cfg) -> int:
    """Resolve matured OPEN predictions and update calibration.

    Returns the number of predictions resolved in this call.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    interval_secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(
        cfg.ohlcv_interval, 300
    )

    open_preds = conn.execute(
        """SELECT id, asset, venue, model_p_up, market_p_up, side, stake_paper,
                  window_close_ts
           FROM predictions
           WHERE status = 'OPEN' AND window_close_ts <= ?""",
        (now_ts,),
    ).fetchall()

    resolved = 0
    for row in open_preds:
        pred_id, asset, venue, model_p_up, market_p_up, side, stake_paper, wc_ts = row

        actual = _get_actual_direction(conn, asset, wc_ts, interval_secs)
        if actual is None:
            continue  # OHLCV data not available yet; try again next cycle

        won = int(side == actual)

        # PnL for a binary contract: win → stake*(1-c)/c, lose → -stake
        if side == "NONE":
            pnl = 0.0
        else:
            c = market_p_up if side == "UP" else (1.0 - market_p_up)
            c = max(c, 1e-6)
            pnl = stake_paper * (1 - c) / c if won else -stake_paper

        conn.execute(
            """INSERT OR REPLACE INTO outcomes
               (prediction_id, resolved_at, actual_direction, won, pnl_paper)
               VALUES (?, ?, ?, ?, ?)""",
            (pred_id, datetime.now(timezone.utc).isoformat(), actual, won, round(pnl, 4)),
        )
        conn.execute(
            "UPDATE predictions SET status = 'RESOLVED' WHERE id = ?", (pred_id,)
        )
        resolved += 1
        logger.info(
            "Resolved prediction %d: %s/%s side=%s actual=%s won=%s pnl=$%.2f",
            pred_id, asset, venue, side, actual, bool(won), pnl,
        )

    if resolved:
        conn.commit()
        for asset in cfg.assets:
            _update_calibration(conn, asset, cfg.risk.kelly_multiplier)

    logger.info("score_predictions: resolved %d predictions", resolved)
    return resolved
