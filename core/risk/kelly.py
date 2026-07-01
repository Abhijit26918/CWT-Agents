"""Kelly-criterion position sizing. MASTER_PLAN.md §2 Agent 4.

Implemented in Phase 3: edge = p - c, f* = (p - c) / (1 - c), DOWN-side
symmetry, fee subtraction, fractional kelly_multiplier, clamp to [0, f_max].
"""


def size_position(*args, **kwargs):
    raise NotImplementedError("Phase 3 — Risk Agent")
