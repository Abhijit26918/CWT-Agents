"""Kalshi market discovery + implied probability. MASTER_PLAN.md §2 Agent 1.

Implemented in Phase 1: series/event/market discovery, yes_bid/yes_ask read.
Kalshi's shortest crypto up/down is ~15m/hourly, not 5m — handled explicitly
by targeting the nearest available horizon.
"""


def get_implied_prob(*args, **kwargs):
    raise NotImplementedError("Phase 1 — Market Agent (Kalshi)")
