"""Shared allocation helpers for the recall-validator.

Cohort selection (one highest-registration miner per coldkey, tie lowest uid), the
per-miner cap constants shared by the incentive v3 allocator and the consumer-side
weight-row verifier, and UID/weight pair normalization for signed envelopes.

This module is pure and offline: no wallet, no Subtensor, no RPC, no network.
"""

from __future__ import annotations

from collections.abc import Sequence

from allocation_types import ScoredMiner, _miner_error

# Per-miner cap = max(CAP_FLOOR, CAP_TWO_OVER_N / n), bounded by the chain cap.
CAP_FLOOR = 0.10
CAP_TWO_OVER_N = 2.0


def normalize_uid_weight_pairs(
    uids: Sequence[int],
    weights: Sequence[float] | Sequence[int],
    hotkeys: Sequence[str] | None = None,
) -> tuple[list[int], list[float] | list[int], list[str] | None]:
    """Normalize parallel UID/weight (optional hotkey) vectors for envelope publish.

    Returns UIDs in ascending unique order. Duplicate UIDs have weights summed
    (first hotkey wins). Used on every encode path so signed
    ``uids`` satisfy bundle_v2 validate_payload (ascending + unique).
    """
    if len(uids) != len(weights):
        raise ValueError("uids/weights length mismatch")
    if hotkeys is not None and len(hotkeys) != len(uids):
        raise ValueError("uids/hotkeys length mismatch")
    if not uids:
        return [], [], None if hotkeys is None else []

    acc_w: dict[int, float | int] = {}
    acc_h: dict[int, str] = {}
    for i, raw_uid in enumerate(uids):
        uid = int(raw_uid)
        w = weights[i]
        if uid in acc_w:
            acc_w[uid] = acc_w[uid] + w  # type: ignore[operator]
        else:
            acc_w[uid] = w
            if hotkeys is not None:
                acc_h[uid] = str(hotkeys[i])
    out_uids = sorted(acc_w.keys())
    out_weights = [acc_w[u] for u in out_uids]
    if hotkeys is None:
        return out_uids, out_weights, None
    return out_uids, out_weights, [acc_h[u] for u in out_uids]



def select_cohort(candidates: Sequence[object]) -> tuple[ScoredMiner, ...]:
    """Dedup to one eligible miner per coldkey (highest registration, tie lowest
    uid), sorted by uid ascending.

    Raises ``ValueError`` on invalid miner metadata (bad uid/coldkey/registration/
    eligible/tier scores) so a future caller cannot silently pass malformed miners
    through to the allocator.
    """
    best: dict[str, ScoredMiner] = {}
    for m in candidates:
        if not isinstance(m, ScoredMiner):
            raise ValueError("candidate must be a ScoredMiner")
        reason = _miner_error(m)
        if reason is not None:
            raise ValueError(reason)
        if not m.eligible:
            continue
        cur = best.get(m.coldkey)
        if cur is None or _beats(m, cur):
            best[m.coldkey] = m
    return tuple(sorted(best.values(), key=lambda m: m.uid))


def _beats(candidate: ScoredMiner, current: ScoredMiner) -> bool:
    if candidate.registration_block != current.registration_block:
        return candidate.registration_block > current.registration_block
    return candidate.uid < current.uid
