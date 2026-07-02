"""Kelly-criterion position sizing. MASTER_PLAN.md §2 Agent 4.

All public functions are pure — no I/O, fully unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass

SIDE_UP = "UP"
SIDE_DOWN = "DOWN"
SIDE_NONE = "NONE"


@dataclass
class Decision:
    side: str           # UP | DOWN | NONE
    edge: float         # signed edge (positive = has edge)
    kelly_fraction: float   # clamped fractional Kelly (0 if NONE)
    stake_paper: float      # dollar amount on paper bankroll


def _kelly_fraction(p: float, c: float) -> float:
    """f* = (p - c) / (1 - c) for a $1-payout binary contract bought at cost c."""
    if c >= 1.0:
        return 0.0
    return (p - c) / (1 - c)


def size_position(
    p_up: float,
    implied_up: float,
    implied_down: float,
    kelly_multiplier: float = 0.25,
    f_max: float = 0.10,
    fee: float = 0.01,
    bankroll: float = 1000.0,
) -> Decision:
    """Decide side and paper stake given model probability and market prices.

    Args:
        p_up:             Model's P(up) in [0, 1].
        implied_up:       Market's implied P(up) (cost of UP contract).
        implied_down:     Market's implied P(down) (cost of DOWN contract).
        kelly_multiplier: Calibration-adjusted multiplier (default 0.25 = quarter-Kelly).
        f_max:            Max fraction of bankroll per bet (safety cap).
        fee:              Spread/fee deducted from the edge before declaring a trade.
        bankroll:         Paper bankroll in dollars.

    Returns:
        Decision with side, edge, kelly_fraction, and paper stake.
    """
    p_down = 1.0 - p_up

    # Evaluate UP side
    edge_up = p_up - implied_up - fee
    f_up = _kelly_fraction(p_up, implied_up) if edge_up > 0 else 0.0

    # Evaluate DOWN side
    edge_down = p_down - implied_down - fee
    f_down = _kelly_fraction(p_down, implied_down) if edge_down > 0 else 0.0

    # Pick the side with the larger post-fee edge
    if edge_up <= 0 and edge_down <= 0:
        return Decision(side=SIDE_NONE, edge=max(edge_up, edge_down),
                        kelly_fraction=0.0, stake_paper=0.0)

    if edge_up >= edge_down:
        raw_f = f_up
        edge = edge_up
        side = SIDE_UP
    else:
        raw_f = f_down
        edge = edge_down
        side = SIDE_DOWN

    clamped = min(kelly_multiplier * raw_f, f_max)
    clamped = max(clamped, 0.0)
    stake = round(clamped * bankroll, 2)

    return Decision(side=side, edge=edge, kelly_fraction=clamped, stake_paper=stake)
