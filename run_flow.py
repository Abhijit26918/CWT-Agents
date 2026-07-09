"""Headless entry point — no LLM. Demo, cron, and free testing.

Usage:
    python run_flow.py --dry                    # print config and exit
    python run_flow.py                          # run one cycle (live Binance OHLCV)
    python run_flow.py --cache                  # dev/offline: frozen Apify fixture, no network
    python run_flow.py --demo                   # demo mode: simulated market odds
    python run_flow.py --loop --interval 300    # run every 5 minutes
    python run_flow.py --demo --confirm --loop  # delayed-confirmation mode (see below)

--demo is useful when Polymarket/Kalshi are geo-blocked (US-restricted APIs).
Kronos runs for real on live OHLCV; market probabilities are simulated to
realistic values. All pipeline logic — prediction, Kelly sizing, persistence —
runs exactly as in production.

By default (no --cache), OHLCV comes from core.data.binance_klines.fetch_ohlcv_live
(free, keyless, not geo-blocked) instead of Apify, so predictions resolve against
real closing bars — this is what lets score_predictions actually accumulate live
calibration data over --loop runs. --cache switches back to the old frozen-fixture
path for fully offline dev (no network at all).

--confirm switches to a delayed-confirmation flow: a 5m candidate with an edge
is parked as PENDING_CONFIRM instead of placed immediately, then ~2 minutes
later re-checked against a 1m model before actually being placed (or dropped).
The confirmation model forecasts confirm-pred-len 1m steps ahead (default 5,
i.e. the same ~5-minute-ahead horizon the original 5m candidate predicted) —
NOT just the next 1-minute bar, which would be a different, much noisier
horizon and not a real confirmation of the same forecast. Only makes sense
with --loop, and needs a faster cadence than the 5-minute horizon window to
catch the confirmation in time — --interval defaults to 60s automatically
when --confirm is set (pass --interval explicitly to override).
"""
from __future__ import annotations

import argparse
import json
import time

from core.config import load_config, load_settings
from core.data.binance_klines import fetch_ohlcv_live
from core.db import init_db
from core.logging_setup import setup_logging
from core.pipeline import run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="CWT crypto predictions — headless runner")
    parser.add_argument("--dry", action="store_true",
                        help="Load config/settings, print summary, and exit")
    parser.add_argument("--cache", action="store_true",
                        help="Offline dev: reuse the frozen Apify fixture instead of "
                             "live Binance OHLCV (no network at all, but predictions "
                             "will never resolve since the fixture never matches real time)")
    parser.add_argument("--demo", action="store_true",
                        help="Use simulated market data (for geo-blocked regions)")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously on --interval seconds")
    parser.add_argument("--interval", type=int, default=None,
                        help="Seconds between loop iterations "
                             "(default: 300, or 60 if --confirm is set)")
    parser.add_argument("--confirm", action="store_true",
                        help="Delayed-confirmation flow: park a candidate, "
                             "re-check with a 1m model ~2min later before placing it")
    parser.add_argument("--confirm-pred-len", type=int, default=5,
                        help="Steps ahead (in 1m units) the confirmation model "
                             "forecasts — should match the candidate's own horizon "
                             "(default 5 = same ~5min-ahead target)")
    args = parser.parse_args()
    interval = args.interval if args.interval is not None else (60 if args.confirm else 300)

    run_id = setup_logging()
    cfg = load_config()
    settings = load_settings()

    if args.dry:
        redacted = {
            "run_id": run_id,
            "config": cfg.model_dump(),
            "secrets_present": {
                "APIFY_TOKEN": settings.apify_token is not None,
                "OPENROUTER_API_KEY": settings.openrouter_api_key is not None,
            },
        }
        print(json.dumps(redacted, indent=2))
        return

    conn = init_db(cfg.db_path)

    demo_finders = None
    if args.demo:
        demo_finders = _build_demo_market_finders()
        print("[ DEMO MODE — simulated market data, real Kronos predictions ]\n")

    def _cycle():
        report = run_once(
            cfg, conn,
            use_cache=args.cache,
            market_finders=demo_finders,
            ohlcv_fetcher=None if args.cache else fetch_ohlcv_live,
            confirm=args.confirm,
            confirm_pred_len=args.confirm_pred_len,
        )
        print()
        report.print_table()
        print()

    if args.confirm and not args.loop:
        print("[ Note: --confirm without --loop only creates candidates — "
              "nothing will be there yet to confirm them ~2min later. ]\n")

    if args.loop:
        print(f"Starting loop — running every {interval}s. Ctrl+C to stop.")
        while True:
            _cycle()
            time.sleep(interval)
    else:
        _cycle()

    conn.close()


def _build_demo_market_finders() -> dict:
    """Return market finders that use realistic simulated probabilities.

    Simulates the kind of tight, near-50% odds you'd see on a real 5m
    crypto up/down market — showing both NO-TRADE and edge scenarios.
    """
    import time as _time
    from core.markets import MarketData

    _DEMO_MARKETS = {
        "BTC": {"polymarket": 0.48, "kalshi": 0.52},
        "ETH": {"polymarket": 0.51, "kalshi": 0.49},
    }

    def _next_boundary(interval_seconds: int = 300) -> int:
        """Next Binance-candle-aligned close time (epoch seconds divisible by
        interval_seconds). score_predictions looks up OHLCV by exact open_time,
        so an unaligned window_close_ts (e.g. plain now()+300) would never
        match a real bar and predictions would never resolve."""
        now = int(_time.time())
        return ((now // interval_seconds) + 1) * interval_seconds

    def _finder(asset: str, horizon: str, venue: str) -> MarketData:
        implied_up = _DEMO_MARKETS.get(asset, {}).get(venue, 0.50)
        return MarketData(
            asset=asset, venue=venue, horizon=horizon,
            up_ref=f"DEMO_{asset}_UP", down_ref=f"DEMO_{asset}_DOWN",
            implied_up=implied_up, implied_down=round(1.0 - implied_up, 4),
            window_close_ts=_next_boundary(300),
            fetched_at="demo",
        )

    return {
        "polymarket": lambda asset, horizon: _finder(asset, horizon, "polymarket"),
        "kalshi":     lambda asset, horizon: _finder(asset, horizon, "kalshi"),
    }


if __name__ == "__main__":
    main()
