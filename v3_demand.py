"""Demand ramp for v3 service pools (pure). Credited demand D per pool per epoch is
computed by recall-web (principal / DB / free caps applied there, numeric only).

    L_e = (1 - lambda) L_{e-1} + lambda D_e
    f_raw = min(D/W, L/W, 1)
    f_e = min(f_raw, f_{e-1} + DELTA_UP)        (no downward limit)
"""

from __future__ import annotations

import math

from v3_params import DEMAND_DELTA_UP, DEMAND_LAMBDA


def _clean(v, hi: float) -> float:
    """Persisted ramp values must be finite and in [0, hi]; anything else restarts at 0
    (a corrupted state can never raise f above a fresh ramp)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if isinstance(v, bool) or not math.isfinite(f) or f < 0 or f > hi:
        return 0.0
    return f


def ramp_step(state: dict | None, D: float, W: float) -> tuple[dict, float]:
    if not (math.isfinite(D) and D >= 0):
        raise ValueError("credited demand must be finite and >= 0")
    if not (math.isfinite(W) and W > 0):
        raise ValueError("W must be finite and > 0")
    L = _clean((state or {}).get("L", 0.0), math.inf)
    f_prev = _clean((state or {}).get("f", 0.0), 1.0)
    L = (1.0 - DEMAND_LAMBDA) * L + DEMAND_LAMBDA * D
    f_raw = min(D / W, L / W, 1.0)
    f = max(0.0, min(f_raw, f_prev + DEMAND_DELTA_UP))
    return {"L": L, "f": f}, f
