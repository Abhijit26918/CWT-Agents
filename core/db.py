"""SQLite connection + idempotent schema migration. See MASTER_PLAN.md §4."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  id INTEGER PRIMARY KEY,
  ts_window INTEGER, asset TEXT, venue TEXT, horizon TEXT,
  up_ref TEXT, down_ref TEXT,
  implied_up REAL, implied_down REAL,
  window_close_ts INTEGER, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS ohlcv (
  asset TEXT, interval TEXT, open_time INTEGER,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
  source TEXT, PRIMARY KEY (asset, interval, open_time)
);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY,
  ts INTEGER, asset TEXT, venue TEXT, horizon TEXT,
  model_p_up REAL, market_p_up REAL, edge REAL,
  side TEXT, kelly_fraction REAL, stake_paper REAL,
  window_close_ts INTEGER, status TEXT DEFAULT 'OPEN', created_at TEXT,
  confirm_at_ts INTEGER, model_p_up_1m REAL
);
CREATE TABLE IF NOT EXISTS outcomes (
  prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),
  resolved_at TEXT, actual_direction TEXT, won INTEGER, pnl_paper REAL
);
CREATE TABLE IF NOT EXISTS calibration (
  asset TEXT PRIMARY KEY, n INTEGER, brier REAL, hit_rate REAL,
  kelly_multiplier REAL DEFAULT 0.25, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
  n_markets INTEGER, n_predictions INTEGER, notes TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add columns introduced after the original CREATE TABLE
    (CREATE TABLE IF NOT EXISTS doesn't alter already-existing tables)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    if "confirm_at_ts" not in cols:
        conn.execute("ALTER TABLE predictions ADD COLUMN confirm_at_ts INTEGER")
    if "model_p_up_1m" not in cols:
        conn.execute("ALTER TABLE predictions ADD COLUMN model_p_up_1m REAL")
    conn.commit()


def init_db(path: str | Path = "cwt.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn
