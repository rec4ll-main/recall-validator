"""Metagraph array normalisation + alignment checks (incentive v3).

bittensor metagraph fields arrive as Python lists (hotkeys, coldkeys, axons) and as numpy
arrays or torch tensors (uids, validator_permit, last_update, block_at_registration)
depending on SDK version and `lite`. Two classic failure modes:

  * truthiness: ``array or []`` raises "truth value of an array ... is ambiguous" for
    length > 1 and silently becomes ``[]`` for a length-1 falsy array;
  * misalignment: index i in one array must be the same neuron as index i in every other
    array, and ``uids[i] == i``; a partially synced metagraph (or an SDK change) can
    return arrays of different lengths, which would pay/slash the wrong neuron.

Everything here is read-only and fails closed (raises MetagraphAlignmentError).
"""

from __future__ import annotations

import operator


class MetagraphAlignmentError(ValueError):
    """Metagraph arrays are missing, of different lengths, or not uid-aligned."""


def as_list(value, name: str = "array") -> list:
    """Return a plain Python list for list/tuple/numpy/torch inputs (None -> [])."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise MetagraphAlignmentError(f"{name}: expected a sequence, got a string")
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        out = tolist()
        if not isinstance(out, list):  # 0-d array -> scalar
            raise MetagraphAlignmentError(f"{name}: expected a 1-d array")
        return out
    try:
        return list(value)
    except TypeError:
        raise MetagraphAlignmentError(f"{name}: not a sequence") from None


def _index(v, name: str) -> int:
    if isinstance(v, bool):
        raise MetagraphAlignmentError(f"invalid {name}")
    try:
        return operator.index(v)
    except TypeError:
        if isinstance(v, float) and v.is_integer():
            return int(v)
        raise MetagraphAlignmentError(f"invalid {name}") from None


def aligned_identity(metagraph) -> tuple[list[str], list[str], list[int]]:
    """Return (hotkeys, coldkeys, registration_blocks) after proving alignment.

    Required: hotkeys, coldkeys, block_at_registration of equal length n > 0.
    When present, uids must be exactly 0..n-1 in order, and n, axons, validator_permit,
    last_update must agree with n."""
    hotkeys = as_list(getattr(metagraph, "hotkeys", None), "hotkeys")
    coldkeys = as_list(getattr(metagraph, "coldkeys", None), "coldkeys")
    regs = as_list(getattr(metagraph, "block_at_registration", None), "block_at_registration")
    n = len(hotkeys)
    if n == 0:
        raise MetagraphAlignmentError("missing metagraph identity")
    if len(coldkeys) != n or len(regs) != n:
        raise MetagraphAlignmentError("metagraph identity length mismatch")
    declared = getattr(metagraph, "n", None)
    if declared is not None:
        item = getattr(declared, "item", None)
        if callable(item):  # numpy 0-d array / torch scalar tensor
            try:
                declared = item()
            except (ValueError, RuntimeError):
                raise MetagraphAlignmentError("metagraph n must be a scalar") from None
        if _index(declared, "n") != n:
            raise MetagraphAlignmentError("metagraph n disagrees with hotkeys length")
    uids_raw = getattr(metagraph, "uids", None)
    if uids_raw is not None:
        uids = [_index(u, "uid") for u in as_list(uids_raw, "uids")]
        if uids != list(range(n)):
            raise MetagraphAlignmentError("metagraph uids are not 0..n-1 in order")
    for field in ("axons", "validator_permit", "last_update"):
        raw = getattr(metagraph, field, None)
        if raw is not None and len(as_list(raw, field)) not in (0, n):
            raise MetagraphAlignmentError(f"metagraph {field} length mismatch")
    reg_values = [_index(r, "registration block") for r in regs]
    for i in range(n):
        if not isinstance(hotkeys[i], str) or not hotkeys[i]:
            raise MetagraphAlignmentError("invalid hotkey")
        if not isinstance(coldkeys[i], str) or not coldkeys[i]:
            raise MetagraphAlignmentError("invalid coldkey")
        if reg_values[i] < 0:
            raise MetagraphAlignmentError("invalid registration block")
    if len(set(hotkeys)) != n:
        raise MetagraphAlignmentError("duplicate hotkey in metagraph")
    return [str(h) for h in hotkeys], [str(c) for c in coldkeys], reg_values
