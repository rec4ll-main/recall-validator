"""Subnet owner UID resolution from finalized chain storage.

Recall never pays the owner UID. Bundle checks use this module to prove the owner
hotkey at one finalized head and refuse any bundle that weights it.
"""

from __future__ import annotations


class OwnerReadError(Exception):
    """The owner chain reads failed; this is not proof that no owner exists."""


def resolve_owner_uid(
    subtensor, metagraph, netuid: int, *, strict: bool = False,
) -> tuple[int | None, str | None]:
    """Prove the registered subnet-owner hotkey at one finalized chain head.

    Sharing the owner's coldkey does not make another hotkey the owner.
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
