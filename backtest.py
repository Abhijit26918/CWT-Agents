"""Historical backtest — headless entry point, mirrors run_flow.py's style.

Usage:
    python backtest.py fetch --asset BTC --days 60
    python backtest.py fetch --asset ETH --days 60

    python backtest.py run --asset BTC --stride 12 --max-windows 500 --seed 42 \\
        --out reports/backtest_BTC.json

    python backtest.py compare --asset BTC --report reports/backtest_BTC.json

`fetch` pulls historical 5m OHLCV directly from Binance's public REST API
(no key needed — Apify's actor has no pagination, see core/data/binance_klines.py)
into a dedicated backtest.db, kept separate from the live cwt.db so a backtest
run can never perturb the live rolling-paper calibration.

`run` replays predict_move + size_position over the fetched history and writes
a JSON report (Brier score, hit rate, synthetic PnL — see core/backtest/engine.py
docstring for why PnL is labeled synthetic).

`compare` prints the backtest report next to the live cwt.db `calibration` +
`outcomes` numbers for the same asset, side by side.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from core.backtest.engine import run_backtest
from core.config import load_config
from core.data.binance_klines import fetch_and_store_history
from core.db import init_db
from core.logging_setup import setup_logging

BACKTEST_DB = "backtest.db"


def cmd_fetch(args: argparse.Namespace) -> None:
    cfg = load_config()
    symbol = cfg.symbols[args.asset]
    conn = init_db(BACKTEST_DB)
    df = fetch_and_store_history(
        asset=args.asset, symbol=symbol, interval=cfg.ohlcv_interval,
        days=args.days, conn=conn,
    )
    conn.close()
    print(f"Fetched {len(df)} historical {cfg.ohlcv_interval} bars for {args.asset} "
          f"({args.days} days) -> {BACKTEST_DB}")


def _load_history(asset: str, interval: str):
    import pandas as pd
    conn = sqlite3.connect(BACKTEST_DB)
    df = pd.read_sql_query(
        """SELECT open_time, open, high, low, close, volume, amount FROM ohlcv
           WHERE asset = ? AND interval = ? ORDER BY open_time ASC""",
        conn, params=(asset, interval),
    )
    conn.close()
    return df


def cmd_run(args: argparse.Namespace) -> None:
    setup_logging()
    cfg = load_config()
    df = _load_history(args.asset, cfg.ohlcv_interval)
    if df.empty:
        raise SystemExit(
            f"No historical data for {args.asset} in {BACKTEST_DB}. "
            f"Run `python backtest.py fetch --asset {args.asset}` first."
        )

    result = run_backtest(
        df, cfg, args.asset,
        market_p_up=args.market_p_up, stride=args.stride,
        max_windows=args.max_windows, seed=args.seed,
    )
    s = result.summary
    print(f"\n=== Backtest: {args.asset} ({s.n_windows} windows, "
          f"{s.n_traded} traded, {s.n_no_trade} no-trade) ===")
    print(f"Brier (all windows):    {s.brier_all}")
    print(f"Hit rate (all windows): {s.hit_rate_all}")
    print(f"Brier (traded only):    {s.brier_traded}")
    print(f"Hit rate (traded only): {s.hit_rate_traded}")
    print(f"Synthetic PnL total:    ${s.total_pnl_synthetic}  "
          f"(market_p_up={args.market_p_up} fixed — not real market odds)")
    print(f"Synthetic max drawdown: ${s.max_drawdown_synthetic}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result.to_dict(), indent=2))
        print(f"\nFull report written to {out_path}")


def cmd_compare(args: argparse.Namespace) -> None:
    report = json.loads(Path(args.report).read_text())
    s = report["summary"]

    conn = sqlite3.connect(args.live_db)
    cal = conn.execute(
        "SELECT n, brier, hit_rate, kelly_multiplier FROM calibration WHERE asset = ?",
        (args.asset,),
    ).fetchone()
    resolved = conn.execute(
        """SELECT COUNT(*) FROM predictions p JOIN outcomes o ON p.id = o.prediction_id
           WHERE p.asset = ? AND p.side != 'NONE'""",
        (args.asset,),
    ).fetchone()[0]
    conn.close()

    print(f"\n=== {args.asset}: backtest vs. live rolling paper ===")
    print(f"{'Metric':<22}{'Backtest':<16}{'Live (rolling paper)'}")
    print("-" * 60)
    print(f"{'N (traded)':<22}{s['n_traded']:<16}{resolved}")
    print(f"{'Brier score':<22}{s['brier_traded']:<16}{cal[1] if cal else 'n/a'}")
    print(f"{'Hit rate':<22}{s['hit_rate_traded']:<16}{cal[2] if cal else 'n/a'}")
    print(f"{'Kelly multiplier':<22}{'n/a (static cfg)':<16}{cal[3] if cal else 'n/a'}")
    if not cal or resolved < 30:
        print(f"\nNote: live sample (N={resolved}) is too small to draw firm "
              f"conclusions from — that's the whole reason the backtest exists.")


def main() -> None:
    parser = argparse.ArgumentParser(description="CWT historical backtest — see module docstring")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Download historical OHLCV from Binance directly")
    p_fetch.add_argument("--asset", required=True, choices=["BTC", "ETH"])
    p_fetch.add_argument("--days", type=int, default=60)
    p_fetch.set_defaults(func=cmd_fetch)

    p_run = sub.add_parser("run", help="Replay the pipeline's prediction+sizing logic over history")
    p_run.add_argument("--asset", required=True, choices=["BTC", "ETH"])
    p_run.add_argument("--stride", type=int, default=12,
                        help="Evaluate every Nth bar (default 12 = hourly on 5m bars)")
    p_run.add_argument("--max-windows", type=int, default=500,
                        help="Cap on windows evaluated (evenly subsampled); real Kronos on CPU is slow")
    p_run.add_argument("--market-p-up", type=float, default=0.50,
                        help="Fixed synthetic market-implied P(up) — no historical odds archive exists")
    p_run.add_argument("--seed", type=int, default=None)
    p_run.add_argument("--out", default=None, help="Path to write the full JSON report")
    p_run.set_defaults(func=cmd_run)

    p_cmp = sub.add_parser("compare", help="Compare a backtest report against live cwt.db")
    p_cmp.add_argument("--asset", required=True, choices=["BTC", "ETH"])
    p_cmp.add_argument("--report", required=True, help="Path to a JSON report from `run`")
    p_cmp.add_argument("--live-db", default="cwt.db")
    p_cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
