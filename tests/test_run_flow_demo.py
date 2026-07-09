"""Regression test for run_flow.py's demo market finder boundary alignment.

score_predictions resolves outcomes by an exact OHLCV open_time lookup
(window_close_ts - interval_seconds). The old demo finder used now()+300,
which is essentially never aligned to a real 5-minute Binance candle boundary
— so demo-mode predictions would silently never resolve, even with live OHLCV.
"""
import time

from run_flow import _build_demo_market_finders


def test_demo_window_close_ts_aligned_to_5m_boundary():
    finders = _build_demo_market_finders()
    market = finders["polymarket"]("BTC", "5m")

    assert market.window_close_ts % 300 == 0


def test_demo_window_close_ts_is_in_the_future():
    finders = _build_demo_market_finders()
    market = finders["kalshi"]("ETH", "5m")

    assert market.window_close_ts > int(time.time())
