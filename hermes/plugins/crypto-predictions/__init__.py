"""Hermes plugin entry point — wires schemas → handlers and registers hooks."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def register(ctx: dict) -> dict:
    """Called by Hermes when the plugin is loaded.

    Args:
        ctx: Hermes plugin context dict. We add our own keys:
             cfg, conn, ohlcv, markets, p_up, use_cache.

    Returns:
        dict with 'tools' mapping and 'hooks' mapping.
    """
    from core.config import load_config, load_settings
    from core.db import init_db
    from core.logging_setup import setup_logging

    from hermes.plugins.crypto_predictions.schemas import SCHEMAS
    from hermes.plugins.crypto_predictions.tools import (
        handle_fetch_ohlcv,
        handle_find_markets,
        handle_predict_move,
        handle_score_predictions,
        handle_size_position,
    )

    setup_logging()
    cfg = load_config()
    load_settings()
    conn = init_db(cfg.db_path)

    ctx.update({"cfg": cfg, "conn": conn, "use_cache": False})

    return {
        "tools": {
            "find_markets":      {"schema": SCHEMAS["find_markets"],      "handler": lambda a: handle_find_markets(a, ctx)},
            "fetch_ohlcv":       {"schema": SCHEMAS["fetch_ohlcv"],       "handler": lambda a: handle_fetch_ohlcv(a, ctx)},
            "predict_move":      {"schema": SCHEMAS["predict_move"],      "handler": lambda a: handle_predict_move(a, ctx)},
            "size_position":     {"schema": SCHEMAS["size_position"],     "handler": lambda a: handle_size_position(a, ctx)},
            "score_predictions": {"schema": SCHEMAS["score_predictions"], "handler": lambda a: handle_score_predictions(a, ctx)},
        },
        "hooks": {
            "post_tool_call": _post_tool_call_hook,
        },
    }


def _post_tool_call_hook(tool_name: str, args: dict, result: dict, latency_ms: float) -> None:
    """Audit log every tool call: tool, args summary, latency, ok/error."""
    ok = "error" not in result
    logger.info(
        "TOOL %s | args=%s | ok=%s | latency=%.0fms",
        tool_name,
        {k: v for k, v in args.items()},
        ok,
        latency_ms,
    )
