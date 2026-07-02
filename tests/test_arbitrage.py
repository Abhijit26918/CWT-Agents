"""Tests for core/scale/arbitrage.py — pure arb math, no network."""
import time

import pytest

from core.markets import MarketData
from core.scale.arbitrage import (
    ArbSignal,
    check_cross_venue_arb,
    check_cross_horizon_consistency,
    scan_arb_opportunities,
)


def _market(asset, venue, implied_up, horizon="5m"):
    return MarketData(
        asset=asset, venue=venue, horizon=horizon,
        up_ref=f"{asset}_up", down_ref=f"{asset}_down",
        implied_up=implied_up, implied_down=1.0 - implied_up,
        window_close_ts=int(time.time()) + 300,
        fetched_at="2025-06-27T05:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Cross-venue arb
# ---------------------------------------------------------------------------

def test_cross_venue_signals_when_spread_exceeds_fees():
    markets = [
        _market("BTC", "polymarket", implied_up=0.60),
        _market("BTC", "kalshi",     implied_up=0.50),
    ]
    # spread = 0.10, fee = 0.02, net_edge = 0.10 - 0.04 = 0.06
    signals = check_cross_venue_arb(markets, fee=0.02)
    assert len(signals) == 1
    assert signals[0].asset == "BTC"
    assert signals[0].kind == "cross_venue"
    assert signals[0].edge == pytest.approx(0.06)
    assert signals[0].buy_venue == "kalshi"   # cheaper UP venue


def test_cross_venue_no_signal_when_spread_within_fees():
    markets = [
        _market("BTC", "polymarket", implied_up=0.52),
        _market("BTC", "kalshi",     implied_up=0.50),
    ]
    # spread = 0.02, fee = 0.02, net_edge = 0.02 - 0.04 = -0.02 → no signal
    signals = check_cross_venue_arb(markets, fee=0.02)
    assert signals == []


def test_cross_venue_no_signal_when_equal():
    markets = [
        _market("BTC", "polymarket", implied_up=0.55),
        _market("BTC", "kalshi",     implied_up=0.55),
    ]
    assert check_cross_venue_arb(markets, fee=0.01) == []


def test_cross_venue_signals_correct_buy_sell_direction():
    # Kalshi prices UP higher → buy polymarket
    markets = [
        _market("BTC", "polymarket", implied_up=0.50),
        _market("BTC", "kalshi",     implied_up=0.65),
    ]
    signals = check_cross_venue_arb(markets, fee=0.02)
    assert len(signals) == 1
    assert signals[0].buy_venue == "polymarket"
    assert signals[0].sell_venue == "kalshi"


def test_cross_venue_handles_multiple_assets():
    markets = [
        _market("BTC", "polymarket", 0.60),
        _market("BTC", "kalshi",     0.50),
        _market("ETH", "polymarket", 0.55),
        _market("ETH", "kalshi",     0.53),   # spread 0.02 < 2×fee
    ]
    signals = check_cross_venue_arb(markets, fee=0.02)
    assets = [s.asset for s in signals]
    assert "BTC" in assets
    assert "ETH" not in assets   # spread too small


# ---------------------------------------------------------------------------
# Cross-horizon consistency
# ---------------------------------------------------------------------------

def test_cross_horizon_flags_large_inconsistency():
    m15 = _market("BTC", "kalshi", implied_up=0.80, horizon="15m")
    m5s = [
        _market("BTC", "polymarket", implied_up=0.50),
        _market("BTC", "polymarket", implied_up=0.50),
        _market("BTC", "polymarket", implied_up=0.50),
    ]
    # joint = 0.5^3 = 0.125, 15m = 0.80, spread = 0.675 >> fee
    sig = check_cross_horizon_consistency(m15, m5s, fee=0.02)
    assert sig is not None
    assert sig.kind == "cross_horizon"
    assert sig.edge > 0


def test_cross_horizon_no_signal_when_consistent():
    m15 = _market("BTC", "kalshi", implied_up=0.52, horizon="15m")
    m5s = [_market("BTC", "polymarket", implied_up=0.80)] * 3
    # joint = 0.512, 15m = 0.52, spread ≈ 0.008 < fee
    sig = check_cross_horizon_consistency(m15, m5s, fee=0.02)
    assert sig is None


# ---------------------------------------------------------------------------
# scan_arb_opportunities convenience wrapper
# ---------------------------------------------------------------------------

def test_scan_returns_all_signals():
    markets = [
        _market("BTC", "polymarket", 0.60, "5m"),
        _market("BTC", "kalshi",     0.50, "5m"),
    ]
    signals = scan_arb_opportunities(markets, fee=0.02)
    assert len(signals) >= 1
    assert all(isinstance(s, ArbSignal) for s in signals)
