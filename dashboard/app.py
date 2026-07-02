"""Streamlit dashboard — live predictions + scoreboard. MASTER_PLAN.md §12.

Run with:
    .venv\\Scripts\\streamlit.exe run dashboard/app.py

Auto-refreshes every 30 seconds.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path("cwt.db")
REFRESH_SECS = 30

st.set_page_config(
    page_title="CWT Crypto Predictions",
    page_icon="📈",
    layout="wide",
)

st.title("CWT Crypto Predictions Agent")
st.caption("Paper trading only — research use. Not financial advice.")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _load_open_predictions(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT asset, venue, horizon, model_p_up, market_p_up, edge,
                  side, stake_paper, created_at
           FROM predictions
           WHERE status = 'OPEN'
           ORDER BY created_at DESC
           LIMIT 50""",
        conn,
    )


def _load_scoreboard(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT asset, n, brier, hit_rate, kelly_multiplier, updated_at
           FROM calibration
           ORDER BY asset""",
        conn,
    )


def _load_pnl_history(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT p.asset, p.venue, o.resolved_at, o.won, o.pnl_paper
           FROM predictions p
           JOIN outcomes o ON p.id = o.prediction_id
           WHERE p.side != 'NONE'
           ORDER BY o.resolved_at""",
        conn,
    )


def _load_recent_resolved(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT p.asset, p.venue, p.side, p.model_p_up, p.market_p_up,
                  o.actual_direction, o.won, o.pnl_paper, o.resolved_at
           FROM predictions p
           JOIN outcomes o ON p.id = o.prediction_id
           ORDER BY o.resolved_at DESC
           LIMIT 20""",
        conn,
    )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

if not DB_PATH.exists():
    st.warning(
        "Database not found. Run `python run_flow.py` at least once to generate predictions."
    )
    st.stop()

conn = _connect()

# ── Top metrics ─────────────────────────────────────────────────────────────
open_df = _load_open_predictions(conn)
score_df = _load_scoreboard(conn)
pnl_df = _load_pnl_history(conn)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Open predictions", len(open_df))
col2.metric("Total resolved", len(pnl_df))

if not pnl_df.empty:
    total_pnl = pnl_df["pnl_paper"].sum()
    col3.metric("Paper PnL ($)", f"{total_pnl:+.2f}")
    win_rate = pnl_df["won"].mean() * 100
    col4.metric("Win rate", f"{win_rate:.1f}%")
else:
    col3.metric("Paper PnL ($)", "—")
    col4.metric("Win rate", "—")

st.divider()

# ── Live predictions ─────────────────────────────────────────────────────────
st.subheader("Live Predictions (OPEN)")
if open_df.empty:
    st.info("No open predictions. Run `python run_flow.py` to generate new ones.")
else:
    def _colour_side(val):
        colour = {"UP": "#1a9e40", "DOWN": "#d62728", "NONE": "#888888"}.get(val, "")
        return f"color: {colour}; font-weight: bold"

    styled = open_df.style.applymap(_colour_side, subset=["side"])
    st.dataframe(styled, use_container_width=True)

st.divider()

# ── Scoreboard ───────────────────────────────────────────────────────────────
st.subheader("Calibration Scoreboard")
if score_df.empty:
    st.info("No calibration data yet — needs resolved predictions.")
else:
    for _, row in score_df.iterrows():
        with st.expander(f"{row['asset']} — Kelly multiplier: {row['kelly_multiplier']:.3f}"):
            c1, c2, c3 = st.columns(3)
            c1.metric("Samples (n)", int(row["n"]))
            c2.metric("Brier score ↓", f"{row['brier']:.3f}")
            c3.metric("Hit rate", f"{row['hit_rate']:.1%}")

st.divider()

# ── Paper PnL curve ──────────────────────────────────────────────────────────
if not pnl_df.empty:
    st.subheader("Cumulative Paper PnL ($)")
    pnl_df["resolved_at"] = pd.to_datetime(pnl_df["resolved_at"])
    pnl_df = pnl_df.sort_values("resolved_at")
    pnl_df["cumulative_pnl"] = pnl_df["pnl_paper"].cumsum()
    st.line_chart(pnl_df.set_index("resolved_at")["cumulative_pnl"])

st.divider()

# ── Recent resolved ──────────────────────────────────────────────────────────
resolved_df = _load_recent_resolved(conn)
if not resolved_df.empty:
    st.subheader("Recent Resolved Predictions")
    st.dataframe(resolved_df, use_container_width=True)

conn.close()

# ── Auto-refresh ─────────────────────────────────────────────────────────────
st.caption(f"Auto-refreshes every {REFRESH_SECS}s")
import time
time.sleep(REFRESH_SECS)
st.rerun()
