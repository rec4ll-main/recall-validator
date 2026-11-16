"""Real installed-SDK encoding + decode + output postconditions for the allocator.

Wraps bittensor's ``convert_and_normalize_weights_and_uids`` — the pure helper the
validator actually uses at emit time, which max-scales then rounds ``*65535`` and
converts float lists through ``np.float32`` as part of the contract — and checks the
quantized output against the declared cap and the real share order. No wallet, no
Subtensor, no RPC, no service import, and no sum==65535 assumption.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from allocation_types import ScoredMiner, _bad_number

U16_MAX = 65535
CAP_TOL = 1e-12
SUM_TOL = 1e-12


def convert_weights_and_uids(
    uids: Sequence[int], weights: Sequence[float]
) -> tuple[list[int], list[int]]:
    """Return ``(weight_uids, weight_vals)`` from the installed SDK codec."""
    from bittensor.utils.weight_utils import convert_and_normalize_weights_and_uids

    return convert_and_normalize_weights_and_uids(list(uids), list(weights))


def decode_shares(weight_vals: Sequence[int]) -> tuple[float, ...]:
    """Decode uint16 values to normalized shares ``q / sum(q)``."""
    total = sum(weight_vals)
    if total <= 0:
        return ()
    return tuple(v / total for v in weight_vals)


def validate_encoding(
    input_uids: Sequence[int],
    weight_uids: Sequence[int],
    weight_vals: Sequence[int],
) -> str | None:
    """Return a reason string if the encoded output is unusable, else ``None``.

    Postconditions: non-empty, uids/values aligned, uids unique and a subset of the
    registered input uids, and every value a non-bool uint16 in range. An empty
    output is always a failure (never ``None``).
    """
    if not weight_uids or not weight_vals:
        return "empty encoded output"
    if len(weight_uids) != len(weight_vals):
        return "uid/value length mismatch"
    if any(isinstance(u, bool) or not isinstance(u, int) for u in weight_uids):
        return "encoded uid must be an int, not bool"
    if len(set(weight_uids)) != len(weight_uids):
        return "duplicate encoded uids"
    registered = set(input_uids)
    if any(uid not in registered for uid in weight_uids):
        return "encoded uid not in registered subset"
    if any(
        isinstance(v, bool) or not isinstance(v, int) or v <= 0 or v > U16_MAX
        for v in weight_vals
    ):
        return "encoded value out of uint16 range"
    return None


def cap_overshoot(decoded: Sequence[float], cap: float) -> bool:
    """True if any decoded share exceeds the declared cap beyond tolerance."""
    return any(s > cap + CAP_TOL for s in decoded)


def check_decoded(
    decoded: Sequence[float],
    cap: float,
    survivors: Sequence[ScoredMiner],
    shares: Sequence[float],
    weight_uids: Sequence[int],
) -> str | None:
    """Return a reason if the SDK-decoded output violates the postconditions.

    Requires decoded shares to be non-empty, finite, positive, summing to 1 within
    tolerance, within the declared cap, and rank-non-inverted against the real share
    order (by value desc, uid asc). A tiny uid dropped by the SDK contributes a zero
    final share and is skipped (a quantization diagnostic, not an inversion).
    """
    if not decoded:
        return "empty decoded shares"
    if any(_bad_number(s) or float(s) <= 0 for s in decoded):
        return "decoded share must be finite and positive"
    if abs(math.fsum(decoded) - 1.0) > SUM_TOL:
        return "decoded share sum != 1"
    if any(float(s) > cap + SUM_TOL for s in decoded):
        return "decoded share exceeds cap"
    decoded_by_uid = dict(zip(weight_uids, decoded))
    order = sorted(range(len(survivors)), key=lambda i: (-shares[i], survivors[i].uid))
    prev = math.inf
    for i in order:
        d = decoded_by_uid.get(survivors[i].uid)
        if d is None:
            continue
        if d > prev + SUM_TOL:
            return "decoded rank inversion"
        prev = d
    return None
