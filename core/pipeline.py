"""Shared orchestration: score → fetch → predict → size → persist.

Called by both run_flow.py (headless) and the Hermes /crypto-flow skill.
All external dependencies are injectable so the pipeline is fully testable.

Optional delayed-confirmation flow (confirm=True): instead of placing a bet
the instant the 5m model forms an edge, park it as a PENDING_CONFIRM
candidate for confirm_delay_seconds, then re-check direction with a faster
1m model (confirm_candidates) before actually placing it (OPEN) or dropping
it (REJECTED). Off by default — existing callers/tests see identical
immediate-placement behavior.
"""
from __future__ import annotations

import logging
import sqlite3
import time
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
    status: str = "OPEN"


@dataclass
class RunReport:
    rows: list[PredictionRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    confirm_counts: dict[str, int] | None = None  # set when run_once(confirm=True)

    def add(self, asset, venue, horizon, p_up, market: MarketData, decision: Decision,
            status: str = "OPEN"):
        self.rows.append(PredictionRow(
            asset=asset, venue=venue, horizon=horizon,
            model_p_up=round(p_up, 4),
            market_p_up=round(market.implied_up, 4),
            edge=round(decision.edge, 4),
            side=decision.side,
            kelly_fraction=round(decision.kelly_fraction, 4),
            stake_paper=decision.stake_paper,
            status=status,
        ))

    def print_table(self):
        if not self.rows:
            print("No predictions generated this run.")
            return
        header = f"{'Asset':<6} {'Venue':<12} {'Model P(up)':<12} {'Market P(up)':<13} {'Edge':<8} {'Side':<6} {'Stake $':<10}{'Status'}"
        print(header)
        print("-" * len(header))
        for r in self.rows:
            print(
                f"{r.asset:<6} {r.venue:<12} {r.model_p_up:<12.4f} "
                f"{r.market_p_up:<13.4f} {r.edge:<8.4f} {r.side:<6} {r.stake_paper:<10.2f}{r.status}"
            )
        if self.confirm_counts:
            c = self.confirm_counts
            print(f"\nConfirmation pass: {c['confirmed']} confirmed, "
                  f"{c['rejected']} rejected, {c['errors']} errors")
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


def _persist_candidate(
    conn: sqlite3.Connection,
    asset: str,
    venue: str,
    horizon: str,
    p_up: float,
    market: MarketData,
    decision: Decision,
    confirm_at_ts: int,
) -> None:
    """Park a candidate as PENDING_CONFIRM instead of placing it immediately.
    confirm_candidates() later either promotes it to OPEN or drops it as
    REJECTED once the 1m confirmation check has run."""
    conn.execute(
        """INSERT INTO predictions
           (ts, asset, venue, horizon, model_p_up, market_p_up, edge,
            side, kelly_fraction, stake_paper, window_close_ts, status,
            created_at, confirm_at_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_CONFIRM', ?, ?)""",
        (
            int(datetime.now(timezone.utc).timestamp()),
            asset, venue, horizon,
            round(p_up, 6), round(market.implied_up, 6), round(decision.edge, 6),
            decision.side, round(decision.kelly_fraction, 6), decision.stake_paper,
            market.window_close_ts,
            datetime.now(timezone.utc).isoformat(),
            confirm_at_ts,
        ),
    )
    conn.commit()


def _has_active_prediction(conn: sqlite3.Connection, asset: str, venue: str) -> bool:
    """True if asset/venue already has a candidate or placed bet still in
    flight (not yet resolved) — guards against generating a new candidate
    every loop tick when the loop runs more often than the horizon window."""
    row = conn.execute(
        """SELECT 1 FROM predictions
           WHERE asset = ? AND venue = ? AND status IN ('PENDING_CONFIRM', 'OPEN')
           LIMIT 1""",
        (asset, venue),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Delayed confirmation: re-check a PENDING_CONFIRM candidate's direction with
# a faster/finer-grained model before actually placing it.
# ---------------------------------------------------------------------------

def confirm_candidates(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    ohlcv_fetcher: Any | None = None,
    predictor: Any | None = None,
    market_finders: dict | None = None,
    apify_client: Any | None = None,
    use_cache: bool = False,
    confirm_interval: str = "1m",
    confirm_pred_len: int = 5,
    now_ts: int | None = None,
) -> dict[str, int]:
    """Confirm or reject PENDING_CONFIRM candidates whose confirm_at_ts has passed.

    For each due candidate: fetch fresh confirm_interval OHLCV, run predict_move
    on it with pred_len=confirm_pred_len steps ahead — matching the same forward
    horizon the original candidate forecast (e.g. 5 one-minute steps ≈ the same
    ~5-minute-ahead target a 5m:n+1 candidate predicted), not just the next
    confirm_interval bar. Checking direction on a mismatched, much shorter
    horizon would be comparing two different forecasts, not confirming one with
    fresher data. If the confirm-interval forecast agrees with the candidate's
    side, re-fetch market odds (may have moved since the candidate was formed)
    and finalize sizing, promoting the row to OPEN. If it disagrees, mark it
    REJECTED — no trade is placed.

    Returns {"confirmed": n, "rejected": n, "errors": n}.
    """
    fetch = ohlcv_fetcher or fetch_ohlcv
    now_ts = now_ts if now_ts is not None else int(time.time())

    if market_finders is None:
        from core.markets.polymarket import find_market as pm_find
        from core.markets.kalshi import find_market as kl_find
        market_finders = {"polymarket": pm_find, "kalshi": kl_find}

    due = conn.execute(
        """SELECT id, asset, venue, horizon, side, model_p_up
           FROM predictions WHERE status = 'PENDING_CONFIRM' AND confirm_at_ts <= ?""",
        (now_ts,),
    ).fetchall()

    counts = {"confirmed": 0, "rejected": 0, "errors": 0}

    for pred_id, asset, venue, horizon, candidate_side, model_p_up in due:
        symbol = cfg.symbols[asset]
        try:
            ohlcv_1m = fetch(
                asset=asset, symbol=symbol,
                interval=confirm_interval, limit=cfg.ohlcv_limit,
                actor_id=cfg.apify.actor, conn=conn,
                client=apify_client, use_cache=use_cache,
            )
            p_up_1m = predict_move(ohlcv_1m, cfg, predictor=predictor,
                                    interval_override=confirm_interval,
                                    pred_len=confirm_pred_len)
        except Exception as exc:
            logger.error("confirm_candidates %s/%s (id=%d): %s", asset, venue, pred_id, exc)
            counts["errors"] += 1
            continue

        one_min_dir = "UP" if p_up_1m >= 0.5 else "DOWN"

        if one_min_dir != candidate_side:
            conn.execute(
                "UPDATE predictions SET status = 'REJECTED', model_p_up_1m = ? WHERE id = ?",
                (round(p_up_1m, 6), pred_id),
            )
            conn.commit()
            counts["rejected"] += 1
            logger.info(
                "Confirmation REJECTED %s/%s (id=%d): candidate=%s 1m_dir=%s p_up_1m=%.3f",
                asset, venue, pred_id, candidate_side, one_min_dir, p_up_1m,
            )
            continue

        try:
            finder = market_finders[venue]
            market = finder(asset, horizon)
            kelly_mult = _get_kelly_multiplier(conn, asset, cfg)
            decision = size_position(
                p_up=model_p_up,
                implied_up=market.implied_up,
                implied_down=market.implied_down,
                kelly_multiplier=kelly_mult,
                f_max=cfg.risk.f_max,
                fee=cfg.risk.fee,
                bankroll=cfg.risk.bankroll_paper,
            )
        except Exception as exc:
            logger.error("confirm_candidates re-price %s/%s (id=%d): %s", asset, venue, pred_id, exc)
            counts["errors"] += 1
            continue

        if decision.side != candidate_side:
            # Market odds moved enough in the confirmation window that the
            # edge no longer favors the confirmed direction — drop it rather
            # than place a trade the 1m check never actually agreed to.
            conn.execute(
                "UPDATE predictions SET status = 'REJECTED', model_p_up_1m = ? WHERE id = ?",
                (round(p_up_1m, 6), pred_id),
            )
            conn.commit()
            counts["rejected"] += 1
            logger.info(
                "Confirmation REJECTED %s/%s (id=%d): 1m agreed but market moved "
                "(candidate=%s, re-priced=%s)",
                asset, venue, pred_id, candidate_side, decision.side,
            )
            continue

        conn.execute(
            """UPDATE predictions SET status = 'OPEN', model_p_up_1m = ?,
               market_p_up = ?, edge = ?, side = ?, kelly_fraction = ?,
               stake_paper = ?, window_close_ts = ? WHERE id = ?""",
            (round(p_up_1m, 6), round(market.implied_up, 6), round(decision.edge, 6),
             decision.side, round(decision.kelly_fraction, 6), decision.stake_paper,
             market.window_close_ts, pred_id),
        )
        conn.commit()
        counts["confirmed"] += 1
        logger.info(
            "Confirmation CONFIRMED %s/%s (id=%d): side=%s p_up_1m=%.3f stake=$%.2f",
            asset, venue, pred_id, decision.side, p_up_1m, decision.stake_paper,
        )

    return counts


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
    ohlcv_fetcher: Any | None = None,
    confirm: bool = False,
    confirm_interval: str = "1m",
    confirm_delay_seconds: int = 120,
    confirm_pred_len: int = 5,
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
        ohlcv_fetcher:  Injectable OHLCV fetcher matching apify_ohlcv.fetch_ohlcv's
                        call signature. Defaults to that real Apify-backed fetch;
                        run_flow.py passes core.data.binance_klines.fetch_ohlcv_live
                        by default so live cycles resolve against real bars instead
                        of a frozen fixture.
        confirm:        If True, use the delayed-confirmation flow: a candidate
                        with an edge is parked as PENDING_CONFIRM instead of
                        placed immediately, and confirm_candidates() is run first
                        to promote/reject any candidates already due. Off by
                        default — existing callers see identical immediate-
                        placement behavior.
        confirm_interval:      OHLCV interval for the confirmation check.
        confirm_delay_seconds: How long a candidate waits before being confirmed.
        confirm_pred_len:      Steps ahead the confirmation model forecasts, in
                               confirm_interval units — should match the original
                               candidate's forward horizon (default 5 × 1m ≈ the
                               same ~5-minute-ahead target as a 5m:n+1 candidate).

    Returns:
        RunReport with one row per asset/venue prediction or candidate.
    """
    fetch = ohlcv_fetcher or fetch_ohlcv
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

    if confirm:
        confirm_counts = confirm_candidates(
            conn, cfg, ohlcv_fetcher=ohlcv_fetcher, predictor=predictor,
            market_finders=market_finders, apify_client=apify_client,
            use_cache=use_cache, confirm_interval=confirm_interval,
            confirm_pred_len=confirm_pred_len,
        )
        report.confirm_counts = confirm_counts

    for asset in cfg.assets:
        symbol = cfg.symbols[asset]

        # Agent 2: fetch OHLCV
        try:
            ohlcv = fetch(
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

            if confirm and _has_active_prediction(conn, asset, venue):
                # Already have a candidate/placed bet in flight for this
                # asset+venue — don't generate another until it resolves.
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

            if confirm and decision.side == "NONE":
                # No edge — nothing to park as a candidate (unlike the
                # immediate-placement path, which still records NONE rows).
                continue

            if confirm:
                confirm_at_ts = int(datetime.now(timezone.utc).timestamp()) + confirm_delay_seconds
                _persist_candidate(conn, asset, venue, market.horizon, p_up, market,
                                    decision, confirm_at_ts)
                report.add(asset, venue, market.horizon, p_up, market, decision,
                           status="PENDING_CONFIRM")
                logger.info(
                    "%s/%s CANDIDATE p_up=%.3f market=%.3f edge=%.3f side=%s "
                    "stake=$%.2f confirm_at=%d",
                    asset, venue, p_up, market.implied_up, decision.edge,
                    decision.side, decision.stake_paper, confirm_at_ts,
                )
            else:
                _persist_prediction(conn, asset, venue, market.horizon, p_up, market, decision)
                report.add(asset, venue, market.horizon, p_up, market, decision)
                logger.info(
                    "%s/%s p_up=%.3f market=%.3f edge=%.3f side=%s stake=$%.2f",
                    asset, venue, p_up, market.implied_up, decision.edge,
                    decision.side, decision.stake_paper,
                )

    return report
