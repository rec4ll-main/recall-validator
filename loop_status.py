"""Operator-facing status of the weight loop: a JSON file in the bundle state directory
rewritten after every tick, and the subnet's MinerBurned share read from chain.

The validator exposes no metrics endpoint; the status file is the hook for alerting
(stale bundles, repeated alerts, MinerBurned at 1.0 which means miners earn nothing).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("recall-validator")

STATUS_FILE = "weight_loop_status.json"
MINER_BURNED_CACHE_SECONDS = 600.0

# MinerBurned decoding is an explicit, versioned adapter, never a guess from magnitude.
# The subnet's MinerBurned storage item is a fixed-point share with 32 fractional bits
# (I96F32): bits / 2**32. That is the representation read on finney at block 9314286,
# where the raw value {'bits': 4294967296} belonged to a subnet whose miner emission was
# entirely burned (share 1.0). A runtime that changes the storage type changes this
# constant in a reviewed release; the status file records which adapter decoded a value.
MINER_BURNED_ENCODING = "I96F32"
MINER_BURNED_FRACTIONAL_BITS = 32

_miner_burned_cache: dict | None = None


def _fixed_to_float(value) -> float | None:
    """Decode the storage value with the MINER_BURNED_ENCODING adapter. Ints and
    {'bits': int} are fixed-point bits; floats are passed through; anything else is
    None. Values above 1.0 are returned as they are so an operator sees them."""
    if isinstance(value, dict):
        value = value.get("bits")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return value / float(2 ** MINER_BURNED_FRACTIONAL_BITS)
    return None


def read_miner_burned(subtensor, netuid: int, now: float | None = None) -> dict:
    """SubtensorModule.MinerBurned for the subnet as a share, with its freshness.

    Returns {"share", "read_at", "encoding", "error", "stale"}: a successful read is
    cached for ten minutes; a failed read keeps the last good share but reports the
    error and marks the value stale, so a consumer can tell "1.0 read just now" from
    "1.0 read an hour ago and failing since". Never raises."""
    global _miner_burned_cache
    now = time.time() if now is None else now
    cached = _miner_burned_cache
    if cached is not None and not cached["error"] and now - cached["read_at"] < MINER_BURNED_CACHE_SECONDS:
        return dict(cached)
    try:
        result = subtensor.substrate.query("SubtensorModule", "MinerBurned", [int(netuid)])
        share = _fixed_to_float(getattr(result, "value", result))
        _miner_burned_cache = {"share": share, "read_at": now, "encoding": MINER_BURNED_ENCODING,
                               "error": None, "stale": False}
    except Exception as exc:  # noqa: BLE001 - status only, never breaks the loop
        logger.warning("MinerBurned read failed: %s", exc)
        base = cached or {"share": None, "read_at": None, "encoding": MINER_BURNED_ENCODING}
        _miner_burned_cache = {"share": base["share"], "read_at": base["read_at"], "encoding": MINER_BURNED_ENCODING,
                               "error": str(exc), "stale": True}
    return dict(_miner_burned_cache)


def write_loop_status(state_dir: str, loop, results, miner_burned) -> str:
    """Atomically rewrite <state_dir>/weight_loop_status.json. Returns the path.

    miner_burned is the dict from read_miner_burned (or a bare share for callers that
    only have the number); the file carries the share, when it was read, the decoding
    adapter and the last error, so a stale or failed read is visible next to the value."""
    if not isinstance(miner_burned, dict):
        miner_burned = {"share": miner_burned, "read_at": None, "encoding": MINER_BURNED_ENCODING,
                        "error": None, "stale": miner_burned is None}
    read_at = miner_burned.get("read_at")
    path = os.path.join(state_dir, STATUS_FILE)
    doc = {
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": [{"mechid": r.mechid, "status": r.status, "reason": r.reason} for r in results],
        "loop": loop.snapshot(),
        "miner_burned": miner_burned.get("share"),
        "miner_burned_read_at": (datetime.fromtimestamp(read_at, timezone.utc).isoformat(timespec="seconds")
                                 if isinstance(read_at, (int, float)) else None),
        "miner_burned_encoding": miner_burned.get("encoding"),
        "miner_burned_error": miner_burned.get("error"),
        "miner_burned_stale": bool(miner_burned.get("stale")),
    }
    try:
        import bundle_health

        doc["bundle_health"] = bundle_health.summary(state_dir)
    except Exception:  # noqa: BLE001 - status only
        doc["bundle_health"] = None
    os.makedirs(state_dir, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    stale = doc["loop"]["stale_mechs"]
    missed = (doc.get("bundle_health") or {}).get("consecutive_missed") or 0
    logger.info("weight loop status: stale_mechs=%s alerts=%s miner_burned=%s stale_read=%s missed_epochs=%s",
                stale, doc["loop"]["alert_counts"], doc["miner_burned"], doc["miner_burned_stale"], missed)
    return path
