"""Headless entry point — no LLM. Demo, cron, and free testing.

Phase 0: only --dry is implemented (loads config + secrets, prints a
redacted summary, exits). --loop/--interval/--cache run the real pipeline
and are wired in Phase 3 once core/pipeline.py exists.
"""
from __future__ import annotations

import argparse
import json

from core.config import load_config, load_settings
from core.logging_setup import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="CWT crypto predictions — headless runner")
    parser.add_argument("--dry", action="store_true", help="Load config/settings and exit, no pipeline run")
    parser.add_argument("--cache", action="store_true", help="Reuse last cached Apify dataset instead of a live call")
    parser.add_argument("--loop", action="store_true", help="Run continuously on --interval seconds")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between loop iterations")
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

    raise NotImplementedError("Phase 3 — Pipeline wiring (run_once / --loop)")


if __name__ == "__main__":
    main()
