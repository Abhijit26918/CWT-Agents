"""Tool handlers — thin wrappers that call core/* and return JSON dicts.

No business logic here. Each handler:
  1. Validates/coerces input
  2. Calls the matching core function
  3. Returns a JSON-serialisable dict

The `ctx` argument is the Hermes plugin context (db connection, config,
loaded OHLCV cache shared across tool calls in one session).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def handle_find_markets(args: dict, ctx: dict) -> dict:
    from core.markets.polymarket import find_market as pm_find
    from core.markets.kalshi import find_market as kl_find
    from core.markets import MarketNotIndexed

    asset = args["asset"].upper()
    horizon = args.get("horizon", ctx["cfg"].horizon)
    results = []

    for venue, finder in [("polymarket", pm_find), ("kalshi", kl_find)]:
        try:
            md = finder(asset, horizon)
            results.append({
                "venue": md.venue,
                "horizon": md.horizon,
                "up_ref": md.up_ref,
                "implied_up": md.implied_up,
                "implied_down": md.implied_down,
                "window_close_ts": md.window_close_ts,
            })
            ctx.setdefault("markets", {})[f"{asset}_{venue}"] = md
        except MarketNotIndexed as exc:
            logger.warning("find_markets %s/%s: %s", asset, venue, exc)
            results.append({"venue": venue, "error": str(exc)})

    return {"asset": asset, "markets": results}


def handle_fetch_ohlcv(args: dict, ctx: dict) -> dict:
    from core.data.apify_ohlcv import fetch_ohlcv

    asset = args["asset"].upper()
    cfg = ctx["cfg"]
    interval = args.get("interval", cfg.ohlcv_interval)
    limit = args.get("limit", cfg.ohlcv_limit)
    symbol = cfg.symbols[asset]

    df = fetch_ohlcv(
        asset=asset, symbol=symbol,
        interval=interval, limit=limit,
        actor_id=cfg.apify.actor,
        conn=ctx["conn"],
        use_cache=ctx.get("use_cache", False),
    )
    ctx.setdefault("ohlcv", {})[asset] = df
    return {"asset": asset, "rows": len(df), "interval": interval}


def handle_predict_move(args: dict, ctx: dict) -> dict:
    from core.predict.kronos_model import predict_move

    asset = args["asset"].upper()
    df = ctx.get("ohlcv", {}).get(asset)
    if df is None:
        return {"error": f"No OHLCV data for {asset} — call fetch_ohlcv first"}

    p_up = predict_move(df, ctx["cfg"])
    ctx.setdefault("p_up", {})[asset] = p_up
    return {"asset": asset, "p_up": round(p_up, 4)}


def handle_size_position(args: dict, ctx: dict) -> dict:
    from core.risk.kelly import size_position
    from core.pipeline import _persist_prediction, _get_kelly_multiplier

    asset = args["asset"].upper()
    venue = args["venue"].lower()
    cfg = ctx["cfg"]
    conn = ctx["conn"]

    p_up = ctx.get("p_up", {}).get(asset)
    market = ctx.get("markets", {}).get(f"{asset}_{venue}")

    if p_up is None:
        return {"error": f"No p_up for {asset} — call predict_move first"}
    if market is None:
        return {"error": f"No market data for {asset}/{venue} — call find_markets first"}

    kelly_mult = _get_kelly_multiplier(conn, asset, cfg)
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

    return {
        "asset": asset,
        "venue": venue,
        "model_p_up": round(p_up, 4),
        "market_p_up": round(market.implied_up, 4),
        "edge": round(decision.edge, 4),
        "side": decision.side,
        "kelly_fraction": round(decision.kelly_fraction, 4),
        "stake_paper": decision.stake_paper,
    }


def handle_score_predictions(args: dict, ctx: dict) -> dict:
    from core.feedback.scoring import score_predictions

    n = score_predictions(ctx["conn"], ctx["cfg"])
    return {"resolved": n, "timestamp": datetime.now(timezone.utc).isoformat()}
