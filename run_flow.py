"""Headless entry point — no LLM. Demo, cron, and free testing.

Usage:
    python run_flow.py --dry                    # print config and exit
    python run_flow.py                          # run one cycle (live APIs)
    python run_flow.py --cache                  # use cached Apify data
    python run_flow.py --demo                   # demo mode: simulated market data
    python run_flow.py --loop --interval 300    # run every 5 minutes

--demo is useful when Polymarket/Kalshi are geo-blocked (US-restricted APIs).
Kronos runs for real on cached OHLCV; market probabilities are simulated to
realistic values. All pipeline logic — prediction, Kelly sizing, persistence —
runs exactly as in production.
"""
from __future__ import annotations

import argparse
import json
import time

from core.config import load_config, load_settings
from core.db import init_db
from core.logging_setup import setup_logging
from core.pipeline import run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="CWT crypto predictions — headless runner")
    parser.add_argument("--dry", action="store_true",
                        help="Load config/settings, print summary, and exit")
    parser.add_argument("--cache", action="store_true",
                        help="Reuse last cached Apify dataset (no Apify credit spend)")
    parser.add_argument("--demo", action="store_true",
                        help="Use simulated market data (for geo-blocked regions)")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously on --interval seconds")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between loop iterations (default: 300)")
    args = parser.parse_args()

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
            use_cache=True,         # always use cache in demo; live otherwise
            market_finders=demo_finders,
        )
        print()
        report.print_table()
        print()

    if args.loop:
        print(f"Starting loop — running every {args.interval}s. Ctrl+C to stop.")
        while True:
            _cycle()
            time.sleep(args.interval)
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

    def _finder(asset: str, horizon: str, venue: str) -> MarketData:
        implied_up = _DEMO_MARKETS.get(asset, {}).get(venue, 0.50)
        return MarketData(
            asset=asset, venue=venue, horizon=horizon,
            up_ref=f"DEMO_{asset}_UP", down_ref=f"DEMO_{asset}_DOWN",
            implied_up=implied_up, implied_down=round(1.0 - implied_up, 4),
            window_close_ts=int(_time.time()) + 300,
            fetched_at="demo",
        )

    return {
        "polymarket": lambda asset, horizon: _finder(asset, horizon, "polymarket"),
        "kalshi":     lambda asset, horizon: _finder(asset, horizon, "kalshi"),
    }


if __name__ == "__main__":
    main()
