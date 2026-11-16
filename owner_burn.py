"""Owner/burn UID resolution from finalized chain storage.

The incentive v3 allocator pays everything it does not pay to a miner to the subnet
owner hotkey's UID; the weight loop falls back to the same UID; consumers verify the
signed burn row against it. This module proves that UID at one finalized head.
"""

from __future__ import annotations


class OwnerReadError(Exception):
    """The owner/burn chain reads failed; this is not proof that no owner exists."""


def resolve_owner_burn_uid(
    subtensor, metagraph, netuid: int, *, strict: bool = False,
) -> tuple[int | None, str | None]:
    """Prove the registered subnet-owner hotkey at one finalized chain head.

    Sharing the owner's coldkey does not make another hotkey a burn target.
    ``metagraph`` remains in the signature for callers but is intentionally not
    consulted: its UID mapping may predate registration changes.

    (None, None) means no owner was proven. By default a failed chain read also
    returns (None, None); with ``strict`` it raises ``OwnerReadError`` instead, so
    a caller can retry rather than treat a transient failure as "no owner".
    """
    from bundle_schedule import finalized_head

    try:
        head = finalized_head(subtensor)

        def read(name, params):
            result = subtensor.substrate.query(
                module="SubtensorModule", storage_function=name,
                params=params, block_hash=head.hash,
            )
            return getattr(result, "value", result)

        hotkey = read("SubnetOwnerHotkey", [netuid])
        if not isinstance(hotkey, str) or not hotkey:
            return None, None
        uid = read("Uids", [netuid, hotkey])
        if isinstance(uid, bool) or not isinstance(uid, int) or not 0 <= uid <= 65535:
            return None, None
        if read("Keys", [netuid, uid]) != hotkey:
            return None, None
    except Exception as exc:
        if strict:
            raise OwnerReadError(f"owner chain read failed: {type(exc).__name__}") from exc
        return None, None
    return uid, hotkey
