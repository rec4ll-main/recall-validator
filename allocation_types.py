"""Frozen cohort value object and input-boundary validation for the chain snapshot.

``ScoredMiner`` is the validated per-UID chain row the snapshot builds its cohort
from (``allocation_snapshot``); the helpers parse untrusted inputs into trusted typed
values. No numeric policy lives here, and no SDK codec lives here (that is
``allocation_encoding.py``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

TIER_COUNT = 4
MAX_CANDIDATES = 4096
UID_MAX = 65535
REG_BLOCK_MAX = 2**64 - 1


@dataclass(frozen=True, slots=True)
class ScoredMiner:
    """A scored, eligibility-flagged miner input (product-service shape, not JSON)."""

    uid: int
    hotkey: str
    coldkey: str
    registration_block: int
    tier_scores: tuple[float, float, float, float]
    eligible: bool


def _bad_number(value: object) -> bool:
    """True if ``value`` is not a finite real number usable in float64 arithmetic.

    bools are rejected; integers too large for float conversion are rejected
    (typed INVALID upstream) rather than raising OverflowError.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    try:
        return not math.isfinite(float(value))
    except OverflowError:
        return True


def _miner_error(m: ScoredMiner) -> str | None:
    if isinstance(m.uid, bool) or not isinstance(m.uid, int) or not 0 <= m.uid <= UID_MAX:
        return f"uid must be an int in [0,{UID_MAX}], not bool"
    if (
        isinstance(m.registration_block, bool)
        or not isinstance(m.registration_block, int)
        or not 0 <= m.registration_block <= REG_BLOCK_MAX
    ):
        return "registration_block must be an int in [0,2^64-1], not bool"
    if not isinstance(m.hotkey, str) or not m.hotkey.strip():
        return "hotkey must be a non-empty string"
    if not isinstance(m.coldkey, str) or not m.coldkey.strip():
        return "coldkey must be a non-empty string"
    if not isinstance(m.eligible, bool):
        return "eligible must be a bool"
    if not isinstance(m.tier_scores, (list, tuple)) or len(m.tier_scores) != TIER_COUNT:
        return "tier_scores must be a 4-tuple"
    if any(_bad_number(s) or float(s) < 0 for s in m.tier_scores):
        return "tier score must be finite and non-negative"
    return None


def _validate_candidates(candidates: Sequence[object]) -> str | None:
    if not isinstance(candidates, (list, tuple)):
        return "candidates must be a list or tuple"
    if len(candidates) > MAX_CANDIDATES:
        return f"too many candidates: {len(candidates)} > {MAX_CANDIDATES}"
    seen_uid: set[int] = set()
    seen_hotkey: set[str] = set()
    for m in candidates:
        if not isinstance(m, ScoredMiner):
            return "candidate must be a ScoredMiner"
        reason = _miner_error(m)
        if reason:
            return reason
        if m.uid in seen_uid:
            return f"duplicate uid: {m.uid}"
        if m.hotkey in seen_hotkey:
            return f"duplicate hotkey: {m.hotkey}"
        seen_uid.add(m.uid)
        seen_hotkey.add(m.hotkey)
    return None
