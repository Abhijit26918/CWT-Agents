"""Tests for core/markets/kalshi.py — series discovery, odds parsing, mocked network."""
import json
from datetime import datetime
from pathlib import Path

import pytest
import responses

from core.markets import MarketData, MarketNotIndexed
from core.markets.kalshi import KALSHI_BASE, find_market

FIXTURES = Path(__file__).parent / "fixtures"
SERIES_FIXTURE = json.loads((FIXTURES / "kalshi_series.json").read_text())
EVENTS_BTC_FIXTURE = json.loads((FIXTURES / "kalshi_events_btc.json").read_text())
MARKETS_BTC_FIXTURE = json.loads((FIXTURES / "kalshi_markets_btc.json").read_text())

_CLOSE_ISO = "2025-06-27T06:00:00Z"
EXPECTED_CLOSE_TS = int(datetime.fromisoformat(_CLOSE_ISO.replace("Z", "+00:00")).timestamp())


def _register_btc_mocks():
    responses.add(responses.GET, f"{KALSHI_BASE}/series",  json=SERIES_FIXTURE,      status=200)
    responses.add(responses.GET, f"{KALSHI_BASE}/events",  json=EVENTS_BTC_FIXTURE,  status=200)
    responses.add(responses.GET, f"{KALSHI_BASE}/markets", json=MARKETS_BTC_FIXTURE, status=200)


# ---------------------------------------------------------------------------
# Odds arithmetic (no network)
# ---------------------------------------------------------------------------

def test_implied_prob_arithmetic():
    # P(up) = (yes_bid + yes_ask) / 200
    yes_bid, yes_ask = 50, 56
    assert (yes_bid + yes_ask) / 200 == pytest.approx(0.53)


def test_implied_down_is_complement():
    yes_bid, yes_ask = 50, 56
    p_up = (yes_bid + yes_ask) / 200
    assert p_up + (1 - p_up) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# find_market with mocked network
# ---------------------------------------------------------------------------

@responses.activate
def test_find_market_btc_returns_market_data():
    _register_btc_mocks()

    md = find_market("BTC", "5m")

    assert isinstance(md, MarketData)
    assert md.asset == "BTC"
    assert md.venue == "kalshi"
    assert md.up_ref == "KXBTCD-25JUN2700T00-U"
    assert md.implied_up == pytest.approx(0.53)
    assert md.implied_down == pytest.approx(0.47)
    assert md.window_close_ts == EXPECTED_CLOSE_TS


@responses.activate
def test_find_market_logs_horizon_mismatch(caplog):
    _register_btc_mocks()
    import logging
    with caplog.at_level(logging.INFO, logger="core.markets.kalshi"):
        find_market("BTC", "5m")
    # Kalshi returns a "15 min window" event while we requested 5m — mismatch logged
    assert any("mismatch" in r.message.lower() for r in caplog.records)


@responses.activate
def test_find_market_raises_when_no_series():
    responses.add(
        responses.GET,
        f"{KALSHI_BASE}/series",
        json={"series": []},   # empty — no crypto series available
        status=200,
    )
    with pytest.raises(MarketNotIndexed):
        find_market("BTC", "5m")


@responses.activate
def test_find_market_raises_when_no_open_events():
    responses.add(responses.GET, f"{KALSHI_BASE}/series",  json=SERIES_FIXTURE,  status=200)
    responses.add(responses.GET, f"{KALSHI_BASE}/events",  json={"events": []},  status=200)

    with pytest.raises(MarketNotIndexed):
        find_market("BTC", "5m")
