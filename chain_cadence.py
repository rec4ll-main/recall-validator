"""Verified storage reads and spec-477 LastUpdate/rate semantics.

Contract: subtensor c94e737f0da25bcbf134fafd6c705a4db5129bfa,
utils/misc.rs:199-208,293-295 and subnets/weights.rs:1295-1314.
A valid short Vec<u64> pads with zero; zero bypasses only the rate limit.
The caller must establish registration and a valid active mechanism at the same
finalized hash first. Transport errors and unverified responses are never defaults.
"""
from __future__ import annotations

from scalecodec.base import ScaleBytes

MECHID_STRIDE = 4096
MAX_MECHANISM_COUNT = 16
U64_MAX = (1 << 64) - 1


class CadenceReadError(ValueError):
    """A fixed, public description of an unavailable or malformed chain value."""


def uint(value, name: str, maximum: int = U64_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CadenceReadError(f"{name} must be an integer")
    if value < 0:
        raise CadenceReadError(f"{name} must be non-negative")
    if value > maximum:
        raise CadenceReadError(f"{name} exceeds its chain bound")
    return value


def mechanism_count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CadenceReadError("MechanismCountCurrent must be an integer")
    if not 1 <= value <= MAX_MECHANISM_COUNT:
        raise CadenceReadError("MechanismCountCurrent must be positive and at most 16")
    return value


def mechanism_index(netuid, mechid, count) -> int:
    uint(netuid, "netuid", MECHID_STRIDE - 1)
    uint(mechid, "mechanism ID", MAX_MECHANISM_COUNT - 1)
    if mechid >= mechanism_count(count):
        raise CadenceReadError("mechanism is not active at the finalized block")
    return netuid + mechid * MECHID_STRIDE


def read_storage(substrate, name: str, params: list, block_hash: str):
    """One explicit successful raw response, decoded with metadata at the same hash.

    In the pinned SDK, query()'s decoder uses response.get('result') and can
    collapse a missing/error envelope into metadata defaults. Keep the raw
    envelope until success has been proved. rpc_request normally raises on those
    failures; the explicit checks also protect against incomplete adapters.
    """
    if not isinstance(block_hash, str) or not block_hash.startswith("0x"):
        raise CadenceReadError(f"{name} requires an explicit finalized hash")
    try:
        key = substrate.create_storage_key(
            pallet="SubtensorModule", storage_function=name,
            params=params, block_hash=block_hash,
        )
        storage_hex = key.to_hex()
        if not isinstance(storage_hex, str) or not storage_hex.startswith("0x"):
            raise CadenceReadError(f"{name} storage key unavailable")
        response = substrate.rpc_request("state_getStorageAt", [storage_hex, block_hash])
        if not isinstance(response, dict) or "error" in response or "result" not in response:
            raise CadenceReadError(f"{name} storage response unverified")
        raw = response["result"]
        if raw is None:
            if name == "LastUpdate" and key.metadata_storage_function.value["modifier"] != "Default":
                raise CadenceReadError("LastUpdate default metadata unavailable")
            encoded = None
        else:
            if (not isinstance(raw, str) or not raw.startswith("0x")
                    or len(raw) % 2 or any(c not in "0123456789abcdefABCDEF" for c in raw[2:])):
                raise CadenceReadError(f"{name} storage bytes malformed")
            encoded = ScaleBytes(raw)
        decoded = key.decode_scale_value(encoded)
        return getattr(decoded, "value", decoded)
    except CadenceReadError:
        raise
    except Exception:
        # RPC/provider exceptions may contain connection details. Never echo them.
        raise CadenceReadError(f"{name} read or decode failed at the finalized block") from None


def last_update_for_uid(row, uid: int, head_block: int) -> int:
    """Read one entry from a verified Vec<u64>; absent tail is chain-default zero."""
    uint(uid, "signer UID", 65535)
    uint(head_block, "finalized block")
    if not isinstance(row, (list, tuple)):
        raise CadenceReadError("LastUpdate unreadable at the finalized block")
    for value in row:
        uint(value, "LastUpdate")
        if value > head_block:
            raise CadenceReadError("LastUpdate is in the future at the finalized block")
    return row[uid] if uid < len(row) else 0


def rate_allows_weights(last_update: int, head_block: int, rate_limit: int) -> bool:
    uint(head_block, "finalized block")
    uint(rate_limit, "WeightsSetRateLimit")
    uint(last_update, "LastUpdate")
    if last_update > head_block:
        raise CadenceReadError("LastUpdate is in the future at the finalized block")
    return last_update == 0 or head_block - last_update >= rate_limit
