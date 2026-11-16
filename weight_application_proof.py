"""Finalized application proof independent of commit/rate-limit activity.

Finney spec475 updates LastUpdate on timelock commit and does not update it on
CR reveal. A same-valued old row plus LastUpdate therefore proves no application.
WeightsSet is emitted when the runtime writes the row. Bound the history work;
missing/pruned history is unproven, never success. Pending queues must be checked
separately before this helper, including before an expired attempt is released.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_APPLICATION_SCAN_BLOCKS = 4096


@dataclass(frozen=True)
class ApplicationProof:
    block: int
    block_hash: str


def _value(obj):
    return getattr(obj, "value", obj)


def _uint(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected unsigned integer in application proof")
    return value


def _query(subtensor, module, name, params, block_hash):
    return _value(subtensor.substrate.query(
        module=module, storage_function=name, params=params, block_hash=block_hash,
    ))


def _matches(event, index, uid):
    event = _value(event)
    if not isinstance(event, dict):
        raise ValueError("invalid event record")
    event = event.get("event", event)
    if not isinstance(event, dict):
        raise ValueError("invalid decoded event")
    if event.get("module_id") != "SubtensorModule" or event.get("event_id") != "WeightsSet":
        return False
    attrs = event.get("attributes")
    # Spec475/477 metadata uses the tuple event WeightsSet(encoded_index, uid).
    # An unknown future schema is not silently treated as application evidence.
    if not isinstance(attrs, (tuple, list)) or len(attrs) != 2:
        raise ValueError("unsupported WeightsSet event attributes")
    return (_uint(attrs[0]), _uint(attrs[1])) == (index, uid)


def find_application_proof(subtensor, *, netuid, mechid, hotkey, uid,
                           expected_row, intent_block, last_update, head):
    """Return (proof, reason); reads only at/below one finalized head.

    The intent block was already finalized before broadcast, so its events are
    excluded. LastUpdate is only a conservative lower bound, never application
    evidence. A matching event also needs the same historical hotkey->UID binding
    and the exact row at that block. No cached event or success boolean is trusted.
    """
    lower = max(intent_block + 1, last_update)
    if lower > head.block:
        return None, "no finalized post-intent application block"
    stop = max(lower, head.block - MAX_APPLICATION_SCAN_BLOCKS + 1)
    index = netuid + mechid * 4096
    try:
        for block in range(head.block, stop - 1, -1):
            block_hash = head.hash if block == head.block else subtensor.get_block_hash(block)
            if not isinstance(block_hash, str) or not block_hash.startswith("0x"):
                raise ValueError("application block hash unavailable")
            events = _query(subtensor, "System", "Events", [], block_hash)
            if not isinstance(events, (tuple, list)):
                raise ValueError("finalized events unavailable")
            if not any(_matches(event, index, uid) for event in events):
                continue
            event_uid = _query(subtensor, "SubtensorModule", "Uids", [netuid, hotkey], block_hash)
            if event_uid is None or _uint(event_uid) != uid:
                continue
            row = _query(subtensor, "SubtensorModule", "Weights", [index, uid], block_hash)
            if row is None:
                row = []
            actual = tuple((_uint(u), _uint(w)) for u, w in row)
            if actual == expected_row:
                return ApplicationProof(block, block_hash), "finalized WeightsSet and exact historical row"
        return None, ("application scan bound reached; application unproven" if stop > lower
                      else "no finalized WeightsSet application proof")
    except Exception:
        # A node with pruned/unavailable historical events cannot prove application.
        # Callers may still expire a mortal attempt once a complete current pending
        # scan is empty; expired is not an applied/confirmed result.
        return None, "finalized application history unavailable or unsupported"
