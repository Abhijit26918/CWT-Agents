"""Shared orchestration: score → fetch → predict → size → persist.

Called by both run_flow.py (headless) and the Hermes /crypto-flow skill.
All external dependencies are injectable so the pipeline is fully testable.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.config import AppConfig
from core.data.apify_ohlcv import fetch_ohlcv
from core.feedback.scoring import score_predictions
from core.markets import MarketData, MarketNotIndexed
from core.predict.kronos_model import predict_move
from core.risk.kelly import Decision, size_position

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunReport — summary returned to callers
# ---------------------------------------------------------------------------

@dataclass
class PredictionRow:
    asset: str
    venue: str
    horizon: str
    model_p_up: float
    market_p_up: float
    edge: float
    side: str
    kelly_fraction: float
    stake_paper: float


@dataclass
class RunReport:
    rows: list[PredictionRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, asset, venue, horizon, p_up, market: MarketData, decision: Decision):
        self.rows.append(PredictionRow(
            asset=asset, venue=venue, horizon=horizon,
            model_p_up=round(p_up, 4),
            market_p_up=round(market.implied_up, 4),
            edge=round(decision.edge, 4),
            side=decision.side,
            kelly_fraction=round(decision.kelly_fraction, 4),
            stake_paper=decision.stake_paper,
        ))

    def print_table(self):
        if not self.rows:
            print("No predictions generated this run.")
            return
        header = f"{'Asset':<6} {'Venue':<12} {'Model P(up)':<12} {'Market P(up)':<13} {'Edge':<8} {'Side':<6} {'Stake $'}"
        print(header)
        print("-" * len(header))
        for r in self.rows:
            print(
                f"{r.asset:<6} {r.venue:<12} {r.model_p_up:<12.4f} "
                f"{r.market_p_up:<13.4f} {r.edge:<8.4f} {r.side:<6} {r.stake_paper:.2f}"
            )
        if self.errors:
            print(f"\nSkipped ({len(self.errors)} errors):")
            for e in self.errors:
                print(f"  • {e}")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist_prediction(
    conn: sqlite3.Connection,
    asset: str,
    venue: str,
    horizon: str,
    p_up: float,
    market: MarketData,
    decision: Decision,
) -> None:
    conn.execute(
        """INSERT INTO predictions
           (ts, asset, venue, horizon, model_p_up, market_p_up, edge,
            side, kelly_fraction, stake_paper, window_close_ts, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
        (
            int(datetime.now(timezone.utc).timestamp()),
            asset, venue, horizon,
            round(p_up, 6), round(market.implied_up, 6), round(decision.edge, 6),
            decision.side, round(decision.kelly_fraction, 6), decision.stake_paper,
            market.window_close_ts,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Kelly multiplier lookup (calibration table, with config fallback)
# ---------------------------------------------------------------------------

def _get_kelly_multiplier(conn: sqlite3.Connection, asset: str, cfg: AppConfig) -> float:
    row = conn.execute(
        "SELECT kelly_multiplier FROM calibration WHERE asset = ?", (asset,)
    ).fetchone()
    return row[0] if row else cfg.risk.kelly_multiplier


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_once(
    cfg: AppConfig,
    conn: sqlite3.Connection,
    venues: tuple[str, ...] | None = None,
    apify_client: Any | None = None,
    predictor: Any | None = None,
    market_finders: dict | None = None,
    use_cache: bool = False,
) -> RunReport:
    """Run one full prediction cycle for all assets × venues.

    Args:
        cfg:            Loaded AppConfig.
        conn:           Open SQLite connection (schema already initialised).
        venues:         Override cfg.venues (useful for tests).
        apify_client:   Injectable Apify client (FakeApifyClient in tests).
        predictor:      Injectable Kronos predictor (FakePredictor in tests).
        market_finders: dict mapping venue name → callable(asset, horizon) → MarketData.
                        Defaults to the real polymarket/kalshi find_market functions.
        use_cache:      Pass True to reuse cached Apify data (dev mode).

    Returns:
        RunReport with one row per asset/venue prediction.
    """
    if venues is None:
        venues = tuple(cfg.venues)

    # Resolve matured predictions from previous runs
    try:
        score_predictions(conn, cfg)
    except NotImplementedError:
        pass  # Phase 4 not yet implemented — skip gracefully

    # Default real market finders
    if market_finders is None:
        from core.markets.polymarket import find_market as pm_find
        from core.markets.kalshi import find_market as kl_find
        market_finders = {"polymarket": pm_find, "kalshi": kl_find}

    report = RunReport()

    for asset in cfg.assets:
        symbol = cfg.symbols[asset]

        # Agent 2: fetch OHLCV
        try:
            ohlcv = fetch_ohlcv(
                asset=asset, symbol=symbol,
                interval=cfg.ohlcv_interval, limit=cfg.ohlcv_limit,
                actor_id=cfg.apify.actor, conn=conn,
                client=apify_client, use_cache=use_cache,
            )
        except Exception as exc:
            msg = f"fetch_ohlcv {asset}: {exc}"
            logger.error(msg)
            report.errors.append(msg)
            continue

        # Agent 3: predict
        try:
            p_up = predict_move(ohlcv, cfg, predictor=predictor)
        except Exception as exc:
            msg = f"predict_move {asset}: {exc}"
            logger.error(msg)
            report.errors.append(msg)
            continue

        kelly_mult = _get_kelly_multiplier(conn, asset, cfg)

        for venue in venues:
            finder = market_finders.get(venue)
            if finder is None:
                continue

            # Agent 1: find market
            try:
                market = finder(asset, cfg.horizon)
            except (MarketNotIndexed, Exception) as exc:
                msg = f"find_market {asset}/{venue}: {exc}"
                logger.warning(msg)
                report.errors.append(msg)
                continue

            # Agent 4: size position
            decision = size_position(
                p_up=p_up,
                implied_up=market.implied_up,
                implied_down=market.implied_down,
                kelly_multiplier=kelly_mult,
                f_max=cfg.risk.f_max,
                fee=cfg.risk.fee,
                bankroll=cfg.risk.bankroll_paper,
            )

            _persist_prediction(conn, asset, venue, market.horizon, p_up, market, decision)
            report.add(asset, venue, market.horizon, p_up, market, decision)
            logger.info(
                "%s/%s p_up=%.3f market=%.3f edge=%.3f side=%s stake=$%.2f",
                asset, venue, p_up, market.implied_up, decision.edge,
                decision.side, decision.stake_paper,
            )

    return report
