"""Headless entry point — no LLM. Demo, cron, and free testing.

Usage:
    python run_flow.py --dry           # print config and exit
    python run_flow.py                 # run one cycle
    python run_flow.py --cache         # use cached Apify data (dev mode)
    python run_flow.py --loop --interval 300   # run every 5 minutes
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

    def _cycle():
        report = run_once(cfg, conn, use_cache=args.cache)
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


if __name__ == "__main__":
    main()
