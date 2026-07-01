"""Tests for core/markets/polymarket.py — slug math, parsing, and mocked network calls."""
import json
from datetime import datetime
from pathlib import Path

import pytest
import responses

from core.markets import MarketData, MarketNotIndexed
from core.markets.polymarket import (
    GAMMA_BASE,
    CLOB_BASE,
    _parse_outcomes,
    _parse_token_ids,
    find_market,
    make_slug,
    window_start,
)

FIXTURES = Path(__file__).parent / "fixtures"
EVENT_FIXTURE = json.loads((FIXTURES / "polymarket_event.json").read_text())

# Unix ts of "2025-06-27T05:05:00Z" — computed the same way the parser does
_END_DATE_ISO = "2025-06-27T05:05:00Z"
EXPECTED_CLOSE_TS = int(datetime.fromisoformat(_END_DATE_ISO.replace("Z", "+00:00")).timestamp())

NOW_TS = 1751000450  # sits inside window starting at 1751000400


# ---------------------------------------------------------------------------
# Slug / window math (no network)
# ---------------------------------------------------------------------------

def test_window_start_5m_floors_to_boundary():
    # 1751000450 // 300 = 5836668, * 300 = 1751000400
    assert window_start("5m", 1751000450) == 1751000400


def test_window_start_on_exact_boundary():
    assert window_start("5m", 1751000400) == 1751000400


def test_window_start_15m():
    # 1751000450 // 900 = 1945556.0, * 900 = 1751000400
    assert window_start("15m", 1751000450) == 1751000400


def test_make_slug_btc():
    assert make_slug("BTC", "5m", 1751000400) == "btc-updown-5m-1751000400"


def test_make_slug_eth():
    assert make_slug("ETH", "15m", 1751000400) == "eth-updown-15m-1751000400"


def test_make_slug_lowercase():
    assert make_slug("btc", "5m", 100) == "btc-updown-5m-100"


# ---------------------------------------------------------------------------
# JSON parsing helpers (no network)
# ---------------------------------------------------------------------------

def test_parse_token_ids_from_json_string():
    assert _parse_token_ids('["aaa","bbb"]') == ["aaa", "bbb"]


def test_parse_token_ids_from_list():
    assert _parse_token_ids(["aaa", "bbb"]) == ["aaa", "bbb"]


def test_parse_outcomes_from_json_string():
    assert _parse_outcomes('["Up","Down"]') == ["Up", "Down"]


def test_parse_outcomes_from_list():
    assert _parse_outcomes(["Up", "Down"]) == ["Up", "Down"]


# ---------------------------------------------------------------------------
# find_market with mocked network
# ---------------------------------------------------------------------------

@responses.activate
def test_find_market_btc_returns_market_data():
    # Gamma API — returns the fixture event list
    responses.add(
        responses.GET,
        f"{GAMMA_BASE}/events",
        json=EVENT_FIXTURE,
        status=200,
    )
    # CLOB midpoint — up token first, down token second (consumed in order)
    responses.add(
        responses.GET,
        f"{CLOB_BASE}/midpoint",
        json={"mid": "0.54"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{CLOB_BASE}/midpoint",
        json={"mid": "0.46"},
        status=200,
    )

    md = find_market("BTC", "5m", now_ts=NOW_TS)

    assert isinstance(md, MarketData)
    assert md.asset == "BTC"
    assert md.venue == "polymarket"
    assert md.horizon == "5m"
    assert md.up_ref == "token_up_111"
    assert md.down_ref == "token_down_222"
    assert md.implied_up == pytest.approx(0.54)
    assert md.implied_down == pytest.approx(0.46)
    assert md.window_close_ts == EXPECTED_CLOSE_TS


@responses.activate
def test_find_market_retries_previous_window_on_empty():
    # First slug returns empty list → retry previous window → returns fixture
    responses.add(
        responses.GET, f"{GAMMA_BASE}/events",
        json=[],       # current window not indexed yet
        status=200,
    )
    responses.add(
        responses.GET, f"{GAMMA_BASE}/events",
        json=EVENT_FIXTURE,   # previous window succeeds
        status=200,
    )
    responses.add(responses.GET, f"{CLOB_BASE}/midpoint", json={"mid": "0.54"}, status=200)
    responses.add(responses.GET, f"{CLOB_BASE}/midpoint", json={"mid": "0.46"}, status=200)

    md = find_market("BTC", "5m", now_ts=NOW_TS)
    assert md.up_ref == "token_up_111"


@responses.activate
def test_find_market_raises_when_both_windows_missing():
    responses.add(responses.GET, f"{GAMMA_BASE}/events", json=[], status=200)
    responses.add(responses.GET, f"{GAMMA_BASE}/events", json=[], status=200)

    with pytest.raises(MarketNotIndexed):
        find_market("BTC", "5m", now_ts=NOW_TS)
