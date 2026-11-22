"""Weight-row validation shared by the master build and the slave consume paths.

Both sides call ``validate_weight_rows`` on the same signed ``uids`` / ``weights_u16``
with the same snapshot native cap, so the master never publishes a bundle that consume
would refuse, and consume never dispatches a vector the chain would reject. Every row is
a miner row: Recall never weights the subnet owner (``owner_row`` finds such a row so
both sides can refuse the bundle).

The native max-weight cap mirrors subtensor ``max_weight_limited`` exactly: the
weights are max-upscaled (identity for an SDK-encoded vector, whose max is 65535),
normalized in I32F32 with truncating division, and the max must be <=
``limit / 65535`` (also I32F32) unless ``limit == u16::MAX``. The check applies to
every row. A vector over the cap is refused, not clamped: clamping would change the
signed allocation.
The chain's single-row self-weight exemption is not applied (no bundle row is the
signing validator's own UID).
"""

from __future__ import annotations

from allocation import CAP_FLOOR, CAP_TWO_OVER_N
from allocation_encoding import convert_weights_and_uids

U16_MAX = 65535
_FRAC_BITS = 32


def native_cap_limit(native_cap: float) -> int:
    """Recover the on-chain u16 ``MaxWeightsLimit`` from the SDK normalized float."""
    limit = round(float(native_cap) * U16_MAX)
    return max(0, min(U16_MAX, limit))


def _fixed_div(numerator: int, denominator: int) -> int:
    """I32F32 raw quotient of two non-negative integers (truncating, like the chain)."""
    if denominator == 0:
        return 0
    return (numerator << _FRAC_BITS) // denominator


def native_cap_violations(weights_u16, limit: int) -> list[int]:
    """Indexes of rows whose chain-normalized share exceeds ``limit / 65535``."""
    if limit >= U16_MAX:
        return []
    total = sum(int(v) for v in weights_u16)
    if total == 0:
        return []
    bound = _fixed_div(limit, U16_MAX)
    return [i for i, v in enumerate(weights_u16) if _fixed_div(int(v), total) > bound]


def miner_cap(native_cap: float, uids) -> float:
    """Per-miner share cap: min(native cap, max(0.10, 2/N)) over the rows."""
    miner_n = max(1, len(uids))
    return min(native_cap, min(1.0, max(CAP_FLOOR, CAP_TWO_OVER_N / miner_n)))


def validate_weight_rows(uids, weights_u16, *, native_cap: float) -> str | None:
    """Return a refusal reason, or None when the signed rows may be dispatched."""
    uids = list(uids)
    weights_u16 = list(weights_u16)
    total = sum(weights_u16)
    if total <= 0:
        return "empty weights"
    shares = [v / total for v in weights_u16]

    limit = native_cap_limit(native_cap)
    for i in native_cap_violations(weights_u16, limit):
        return (
            f"row uid {uids[i]} share {shares[i]:.6f} exceeds native "
            f"max-weight cap {native_cap:.6f}"
        )

    cap = miner_cap(native_cap, uids)
    for share in shares:
        if share > cap + 1e-9:
            return "decoded share exceeds cap"

    re_uids, re_values = convert_weights_and_uids(uids, shares)
    if re_uids != uids or re_values != weights_u16:
        return "SDK encode equality failed"
    return None


def owner_row(subtensor, metagraph, netuid: int, payload, *, strict: bool = False) -> int | None:
    """UID of a signed row that pays the subnet owner hotkey, else None. Recall never
    weights the owner, so a bundle with such a row is refused.

    With ``strict`` a failed owner read raises ``OwnerReadError`` (see
    ``resolve_owner_uid``) instead of returning None.
    """
    from owner_uid import resolve_owner_uid

    owner_uid, owner_hotkey = resolve_owner_uid(subtensor, metagraph, netuid, strict=strict)
    if owner_hotkey is None:
        return None
    uids = list(getattr(payload, "uids", ()) or ())
    hotkeys = list(getattr(payload, "hotkeys", ()) or ())
    for uid, hotkey in zip(uids, hotkeys):
        if hotkey == owner_hotkey or uid == owner_uid:
            return int(uid)
    return None
