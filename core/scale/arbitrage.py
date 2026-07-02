"""Cross-venue and cross-horizon arbitrage detection. MASTER_PLAN.md §12.

Two checks:
1. Cross-venue: Polymarket vs Kalshi implied P(up) for the same asset.
   If they diverge by more than fees, flag the cheaper venue as the entry.
2. Cross-horizon consistency: a 15m market should roughly equal the product
   of three 5m market probabilities. A large inconsistency flags an edge.

Both functions are pure (no I/O) — fully unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.markets import MarketData


@dataclass
class ArbSignal:
    asset: str
    kind: str            # "cross_venue" | "cross_horizon"
    description: str
    edge: float          # estimated edge available (positive = opportunity)
    buy_venue: str       # venue/side to buy
    sell_venue: str      # venue/side to sell (or "N/A" for cross-horizon)


# ---------------------------------------------------------------------------
# Cross-venue arbitrage
# ---------------------------------------------------------------------------

def check_cross_venue_arb(
    markets: list[MarketData],
    fee: float = 0.02,
) -> list[ArbSignal]:
    """Compare Polymarket vs Kalshi implied P(up) for each asset.

    If one venue prices UP higher than the other by more than 2× fee
    (round-trip cost), flag it: buy the cheaper venue, note the spread.

    Args:
        markets: List of MarketData from both venues for one or more assets.
        fee:     One-way fee/spread to deduct (default 0.02 = 2%).

    Returns:
        List of ArbSignal (empty if no opportunity).
    """
    by_asset: dict[str, dict[str, MarketData]] = {}
    for md in markets:
        by_asset.setdefault(md.asset, {})[md.venue] = md

    signals = []
    for asset, venue_map in by_asset.items():
        pm = venue_map.get("polymarket")
        kl = venue_map.get("kalshi")
        if pm is None or kl is None:
            continue

        spread = pm.implied_up - kl.implied_up
        net_edge = abs(spread) - 2 * fee  # round-trip cost

        if net_edge <= 0:
            continue

        if spread > 0:
            # Polymarket prices UP higher → buy UP on Kalshi (cheaper)
            buy_venue, sell_venue = "kalshi", "polymarket"
        else:
            buy_venue, sell_venue = "polymarket", "kalshi"

        signals.append(ArbSignal(
            asset=asset,
            kind="cross_venue",
            description=(
                f"{asset}: Polymarket P(up)={pm.implied_up:.3f} vs "
                f"Kalshi P(up)={kl.implied_up:.3f} | spread={abs(spread):.3f} "
                f"net_edge={net_edge:.3f}"
            ),
            edge=round(net_edge, 4),
            buy_venue=buy_venue,
            sell_venue=sell_venue,
        ))

    return signals


# ---------------------------------------------------------------------------
# Cross-horizon consistency check (15m vs 3 × 5m)
# ---------------------------------------------------------------------------

def check_cross_horizon_consistency(
    market_15m: MarketData,
    markets_5m: list[MarketData],
    fee: float = 0.02,
) -> ArbSignal | None:
    """Check whether a 15m market is consistent with three 5m markets.

    The 15m UP probability should be approximately equal to the joint
    probability of three consecutive 5m UP moves (simplified: product of
    the three P(up) values under independence assumption).

    A large inconsistency signals a potential edge.

    Args:
        market_15m:  MarketData for the 15m up/down market.
        markets_5m:  List of up to 3 consecutive 5m MarketData.
        fee:         Round-trip fee threshold.

    Returns:
        ArbSignal if inconsistency exceeds fee, else None.
    """
    if not markets_5m:
        return None

    # Joint 5m probability (independent assumption — an approximation)
    p_joint_up = 1.0
    for md in markets_5m[:3]:
        p_joint_up *= md.implied_up

    implied_15m = market_15m.implied_up
    spread = abs(implied_15m - p_joint_up)
    net_edge = spread - 2 * fee

    if net_edge <= 0:
        return None

    return ArbSignal(
        asset=market_15m.asset,
        kind="cross_horizon",
        description=(
            f"{market_15m.asset}: 15m P(up)={implied_15m:.3f} vs "
            f"3×5m joint P(up)={p_joint_up:.3f} | spread={spread:.3f} "
            f"net_edge={net_edge:.3f}"
        ),
        edge=round(net_edge, 4),
        buy_venue=market_15m.venue if implied_15m < p_joint_up else "5m_markets",
        sell_venue="5m_markets" if implied_15m < p_joint_up else market_15m.venue,
    )


# ---------------------------------------------------------------------------
# Convenience: run all arb checks and return summary
# ---------------------------------------------------------------------------

def scan_arb_opportunities(
    markets: list[MarketData],
    fee: float = 0.02,
) -> list[ArbSignal]:
    """Run all available arbitrage checks and return combined signals."""
    signals = check_cross_venue_arb(markets, fee=fee)

    # Cross-horizon: group by asset
    by_asset: dict[str, list[MarketData]] = {}
    for md in markets:
        by_asset.setdefault(md.asset, []).append(md)

    for asset, asset_markets in by_asset.items():
        m15 = next((m for m in asset_markets if m.horizon == "15m"), None)
        m5s = [m for m in asset_markets if m.horizon == "5m"]
        if m15 and m5s:
            sig = check_cross_horizon_consistency(m15, m5s, fee=fee)
            if sig:
                signals.append(sig)

    return signals
