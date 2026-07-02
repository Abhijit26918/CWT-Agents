"""Tests for core/risk/kelly.py — pure function, no I/O."""
import pytest

from core.risk.kelly import Decision, SIDE_DOWN, SIDE_NONE, SIDE_UP, size_position


# ---------------------------------------------------------------------------
# Edge cases: no trade
# ---------------------------------------------------------------------------

def test_no_trade_when_model_equals_market():
    d = size_position(p_up=0.50, implied_up=0.50, implied_down=0.50, fee=0.0)
    assert d.side == SIDE_NONE
    assert d.stake_paper == 0.0


def test_no_trade_when_fee_kills_both_sides():
    # High fee (0.10) wipes out a 5% gross edge on both sides
    # edge_up = 0.55 - 0.50 - 0.10 = -0.05 ≤ 0
    # edge_down = 0.45 - 0.50 - 0.10 = -0.15 ≤ 0
    d = size_position(p_up=0.55, implied_up=0.50, implied_down=0.50, fee=0.10)
    assert d.side == SIDE_NONE


def test_no_trade_when_model_inside_market_spread():
    # p_up=0.51, market tight: implied_up=0.50, implied_down=0.51, fee=0.02
    # edge_up = 0.51 - 0.50 - 0.02 = -0.01 ≤ 0
    # edge_down = 0.49 - 0.51 - 0.02 = -0.04 ≤ 0
    d = size_position(p_up=0.51, implied_up=0.50, implied_down=0.51, fee=0.02)
    assert d.side == SIDE_NONE


# ---------------------------------------------------------------------------
# UP side
# ---------------------------------------------------------------------------

def test_up_side_when_model_above_market():
    # p=0.60, c=0.50 → f*=(0.60-0.50)/(1-0.50)=0.20, quarter-Kelly=0.05
    d = size_position(p_up=0.60, implied_up=0.50, implied_down=0.50,
                      kelly_multiplier=0.25, f_max=0.10, fee=0.01, bankroll=1000.0)
    assert d.side == SIDE_UP
    assert d.kelly_fraction > 0
    assert d.stake_paper > 0


def test_up_kelly_formula():
    # f* = (p-c)/(1-c) = (0.60-0.50)/0.50 = 0.20, × 0.25 = 0.05
    d = size_position(p_up=0.60, implied_up=0.50, implied_down=0.50,
                      kelly_multiplier=0.25, f_max=0.10, fee=0.0, bankroll=1000.0)
    assert d.kelly_fraction == pytest.approx(0.05)
    assert d.stake_paper == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# DOWN side
# ---------------------------------------------------------------------------

def test_down_side_when_model_predicts_down():
    # p_up=0.30, p_down=0.70, implied_down=0.50 → DOWN has edge
    d = size_position(p_up=0.30, implied_up=0.50, implied_down=0.50,
                      kelly_multiplier=0.25, f_max=0.10, fee=0.01, bankroll=1000.0)
    assert d.side == SIDE_DOWN
    assert d.stake_paper > 0


def test_down_kelly_formula():
    # p_down=0.70, c_down=0.50 → f*=(0.70-0.50)/0.50=0.40, ×0.25=0.10 (caps at f_max=0.10)
    d = size_position(p_up=0.30, implied_up=0.50, implied_down=0.50,
                      kelly_multiplier=0.25, f_max=0.10, fee=0.0, bankroll=1000.0)
    assert d.kelly_fraction == pytest.approx(0.10)   # capped at f_max
    assert d.stake_paper == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Clamping / caps
# ---------------------------------------------------------------------------

def test_stake_capped_at_f_max():
    # Very large edge: ensure stake never exceeds f_max × bankroll
    d = size_position(p_up=0.99, implied_up=0.01, implied_down=0.99,
                      kelly_multiplier=1.0, f_max=0.10, fee=0.0, bankroll=1000.0)
    assert d.kelly_fraction <= 0.10
    assert d.stake_paper <= 100.0


def test_stake_never_negative():
    d = size_position(p_up=0.0, implied_up=0.5, implied_down=0.5,
                      kelly_multiplier=0.25, f_max=0.10, fee=0.0)
    assert d.stake_paper >= 0.0


def test_reduced_kelly_multiplier_reduces_stake():
    d_full = size_position(p_up=0.60, implied_up=0.50, implied_down=0.50,
                           kelly_multiplier=1.0, f_max=0.50, fee=0.0)
    d_quarter = size_position(p_up=0.60, implied_up=0.50, implied_down=0.50,
                              kelly_multiplier=0.25, f_max=0.50, fee=0.0)
    assert d_quarter.stake_paper < d_full.stake_paper


# ---------------------------------------------------------------------------
# Edge arithmetic
# ---------------------------------------------------------------------------

def test_edge_is_positive_for_up_trade():
    d = size_position(p_up=0.60, implied_up=0.50, implied_down=0.50, fee=0.01)
    assert d.edge > 0


def test_edge_is_fee_adjusted():
    # Gross edge = 0.10, fee = 0.03 → net edge = 0.07
    d = size_position(p_up=0.60, implied_up=0.50, implied_down=0.50, fee=0.03)
    assert d.edge == pytest.approx(0.07, abs=1e-9)
