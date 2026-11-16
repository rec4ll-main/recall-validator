"""Immutable chain-cohort snapshot for the allocation-v2 submission path.

Every build refreshes the metagraph at the current finalized head, pins the native
chain max weight to that same block, and freezes the COMPLETE UID/hotkey/coldkey/
registration mapping plus the dedup cohort. Rechecking after a score fetch compares
the full identity and cap, so an advancing head with unchanged identity is allowed
while any identity/cap change aborts.
"""

from __future__ import annotations

import hashlib
import json
import math
import operator
from dataclasses import dataclass

from allocation import select_cohort
from allocation_types import ScoredMiner, _validate_candidates
from metagraph_arrays import MetagraphAlignmentError, aligned_identity


class AllocationSnapshotError(Exception):
    """A missing, invalid, or mismatched chain identity or native cap."""


@dataclass(frozen=True, slots=True)
class AllocationSnapshot:
    netuid: int
    anchor_hash: str
    anchor_block: int
    native_cap: float
    hotkeys: tuple[str, ...]
    coldkeys: tuple[str, ...]
    registration_blocks: tuple[int, ...]
    cohort: tuple[tuple[int, str, str, int], ...]


def snapshot_identity_changed(a: AllocationSnapshot, b: AllocationSnapshot) -> bool:
    """True if the full identity mapping or native cap changed (anchor block
    advance is ignored)."""
    return (
        a.netuid != b.netuid
        or a.native_cap != b.native_cap
        or a.hotkeys != b.hotkeys
        or a.coldkeys != b.coldkeys
        or a.registration_blocks != b.registration_blocks
    )


def _to_index(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise AllocationSnapshotError(f"invalid {name}")
    try:
        return operator.index(value)
    except TypeError:
        raise AllocationSnapshotError(f"invalid {name}") from None


def snapshot_digest(snapshot: AllocationSnapshot) -> str:
    """Canonical public digest of the full identity mapping plus native cap.

    This is the shared digest both roles derive from the same pinned snapshot, so a
    tampered or divergent mapping/cap produces a different digest.
    """
    canonical = {
        "netuid": snapshot.netuid,
        "native_cap": repr(snapshot.native_cap),
        "hotkeys": list(snapshot.hotkeys),
        "coldkeys": list(snapshot.coldkeys),
        "registration_blocks": list(snapshot.registration_blocks),
    }
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "0x" + hashlib.sha256(data).hexdigest()


def build_snapshot(
    subtensor,
    metagraph,
    netuid: int,
    active_miners: list[tuple[int, str]] | None,
    expected_genesis_hash: str,
    block: int | None = None,
) -> AllocationSnapshot:
    """Refresh the metagraph at the finalized head (or an explicit block) and
    freeze identity + cap. ``block`` pins the snapshot to a specific finalized
    epoch block for both roles. When ``active_miners`` is None, the dedup cohort is
    derived from the full metagraph identity."""
    if block is not None:
        block_number = _to_index(block, "block")
        try:
            head_hash = subtensor.substrate.get_chain_finalised_head()
            head_number = _to_index(
                subtensor.substrate.get_block_number(head_hash), "finalized block number"
            )
        except AllocationSnapshotError:
            raise
        except Exception as exc:
            raise AllocationSnapshotError(f"finalized head unavailable: {exc}") from exc
        if block_number > head_number:
            raise AllocationSnapshotError(
                f"block {block_number} is above the finalized head {head_number}"
            )
        try:
            finalized_hash = subtensor.get_block_hash(block_number)
        except Exception as exc:
            raise AllocationSnapshotError(f"block hash unavailable: {exc}") from exc
    else:
        try:
            finalized_hash = subtensor.substrate.get_chain_finalised_head()
            block_number = _to_index(subtensor.substrate.get_block_number(finalized_hash), "block number")
        except AllocationSnapshotError:
            raise
        except Exception as exc:
            raise AllocationSnapshotError(f"finalized head unavailable: {exc}") from exc

    try:
        genesis = subtensor.get_block_hash(0)
    except Exception as exc:
        raise AllocationSnapshotError(f"genesis unavailable: {exc}") from exc
    if not expected_genesis_hash or genesis != expected_genesis_hash:
        raise AllocationSnapshotError("genesis hash mismatch")

    try:
        metagraph.sync(subtensor=subtensor, block=block_number, lite=True)
    except Exception as exc:
        raise AllocationSnapshotError(f"metagraph sync failed: {exc}") from exc

    try:
        native_cap = subtensor.max_weight_limit(netuid, block=block_number)
    except Exception as exc:
        raise AllocationSnapshotError(f"native cap unavailable: {exc}") from exc
    if native_cap is None:
        raise AllocationSnapshotError("native chain cap unavailable")
    if isinstance(native_cap, bool):
        raise AllocationSnapshotError("native chain cap invalid")
    try:
        native_cap = float(native_cap)
    except (TypeError, ValueError, OverflowError):
        raise AllocationSnapshotError("native chain cap invalid") from None
    if not math.isfinite(native_cap) or not (0 < native_cap <= 1):
        raise AllocationSnapshotError("native chain cap out of range")

    # Normalise numpy/torch/list arrays and prove uid/hotkey/coldkey/registration (and,
    # when present, uids/axons/permit/last_update) alignment; fail closed otherwise.
    try:
        hotkeys, coldkeys, reg_values = aligned_identity(metagraph)
    except MetagraphAlignmentError as exc:
        raise AllocationSnapshotError(str(exc)) from None

    if active_miners is None:
        active_miners = [(i, hotkeys[i]) for i in range(len(hotkeys))]

    miners: list[ScoredMiner] = []
    for uid, hotkey in active_miners:
        uid_idx = _to_index(uid, "uid")
        if uid_idx < 0 or uid_idx >= len(hotkeys):
            raise AllocationSnapshotError("uid out of range")
        if hotkeys[uid_idx] != hotkey:
            raise AllocationSnapshotError("hotkey mismatch")
        miners.append(
            ScoredMiner(
                uid=uid_idx,
                hotkey=hotkey,
                coldkey=coldkeys[uid_idx],
                registration_block=reg_values[uid_idx],
                tier_scores=(0.0, 0.0, 0.0, 0.0),
                eligible=True,
            )
        )

    reason = _validate_candidates(miners)
    if reason:
        raise AllocationSnapshotError(reason)
    cohort = select_cohort(miners)
    if not cohort:
        raise AllocationSnapshotError("empty cohort")

    return AllocationSnapshot(
        netuid=netuid,
        anchor_hash=finalized_hash,
        anchor_block=block_number,
        native_cap=native_cap,
        hotkeys=tuple(hotkeys),
        coldkeys=tuple(coldkeys),
        registration_blocks=tuple(reg_values),
        cohort=tuple((m.uid, m.hotkey, m.coldkey, m.registration_block) for m in cohort),
    )
