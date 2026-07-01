"""Shared orchestration used by both run_flow.py and the Hermes /crypto-flow skill.

Implemented in Phase 3: run_once(cfg, db, venues) -> RunReport, wiring
score_predictions -> fetch_ohlcv -> predict_move -> find_market -> size_position
per asset/venue per MASTER_PLAN.md §7.
"""


def run_once(*args, **kwargs):
    raise NotImplementedError("Phase 3 — Pipeline")
