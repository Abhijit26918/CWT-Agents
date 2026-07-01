"""Rotating file + console logging, shared across run_flow.py and the Hermes plugin."""
from __future__ import annotations

import logging
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: str | Path = "logs", level: int = logging.INFO) -> str:
    """Configures the root logger. Returns a run_id to thread through this run."""
    run_id = uuid.uuid4().hex[:8]
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        f"%(asctime)s [run={run_id}] %(levelname)s %(name)s: %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_path / "cwt.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    return run_id
