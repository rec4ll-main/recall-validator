"""Per-epoch bundle health: did this epoch's weight bundle get published and consumed?

Every validator consumes the master's bundle once the epoch's publication cutoff is
finalized; the master also publishes it before that. When the master cannot publish (for
example the API's evidence request times out), the weight loop keeps re-setting the last
valid bundle and logs ``bundle_failclosed``, which reads the same whether the bundle is a
few blocks late or the epoch was missed entirely.

This module records, per epoch, the master's publish attempts and every consume result,
and decides once per epoch:

  * consumed: a consume returned applied / verified / pending -> healthy;
  * missed:   MISS_AFTER_TICKS consecutive consume results after the cutoff were none of
              those -> one ``ALERT bundle_missed`` log line naming the last consume and
              publish reasons, and the epoch counts as missed.

A missed epoch that is consumed later is logged as recovered. The state file
(bundle_health.json in the bundle state directory) carries the counters an operator or a
monitor reads: consecutive missed epochs, total missed, the last missed and the last
consumed epoch, and the last reasons. Never raises into the validator loop.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("recall-validator")

STATUS_FILE = "bundle_health.json"
# Consume runs every validator tick (~15 s); five unsuccessful results in a row after the
# cutoff (~75 s) without one success means the epoch's bundle is not there.
MISS_AFTER_TICKS = 5
CONSUMED = ("applied", "verified", "pending")
PUBLISHED = ("published", "pending")

_FRESH = {
    "epoch": None, "consumed": False, "missed": False, "bad_ticks": 0,
    "publish_attempts": 0, "published": False, "last_publish": "", "last_consume": "",
    "consecutive_missed": 0, "missed_total": 0, "last_missed_epoch": None,
    "last_consumed_epoch": None, "updated_at": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(state_dir: str) -> str:
    return os.path.join(state_dir, STATUS_FILE)


def read_health(state_dir: str) -> dict:
    """The stored state, or a fresh one when the file is missing or unreadable."""
    try:
        with open(_path(state_dir), encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict):
            return {**_FRESH, **doc}
    except (OSError, ValueError):
        pass
    return dict(_FRESH)


def _write(state_dir: str, doc: dict) -> None:
    doc["updated_at"] = _now()
    os.makedirs(state_dir, exist_ok=True)
    path = _path(state_dir)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _for_epoch(doc: dict, epoch: int) -> dict:
    """Start a new epoch's per-epoch fields, keeping the counters."""
    if doc.get("epoch") != epoch:
        doc.update({"epoch": int(epoch), "consumed": False, "missed": False, "bad_ticks": 0,
                    "publish_attempts": 0, "published": False, "last_publish": "", "last_consume": ""})
    return doc


def record_publish(state_dir: str, epoch: int, status: str, reason: str) -> dict:
    """Record one master publish attempt for ``epoch``."""
    doc = _for_epoch(read_health(state_dir), epoch)
    doc["publish_attempts"] += 1
    doc["published"] = doc["published"] or status in PUBLISHED
    doc["last_publish"] = f"{status}: {reason}".strip().rstrip(":")[:300]
    _write(state_dir, doc)
    return doc


def record_consume(state_dir: str, epoch: int, status: str, reason: str) -> dict:
    """Record one consume result for ``epoch`` (after its cutoff) and decide the epoch."""
    doc = _for_epoch(read_health(state_dir), epoch)
    doc["last_consume"] = f"{status}: {reason}".strip().rstrip(":")[:300]
    if status in CONSUMED:
        if not doc["consumed"]:
            doc["consumed"] = True
            doc["last_consumed_epoch"] = int(epoch)
            if doc["missed"]:
                logger.warning("bundle health: epoch %d bundle consumed after it was reported missed", epoch)
            doc["consecutive_missed"] = 0
        doc["bad_ticks"] = 0
    elif not doc["consumed"] and not doc["missed"]:
        doc["bad_ticks"] += 1
        if doc["bad_ticks"] >= MISS_AFTER_TICKS:
            doc["missed"] = True
            doc["consecutive_missed"] += 1
            doc["missed_total"] += 1
            doc["last_missed_epoch"] = int(epoch)
            publish = ""
            if doc["publish_attempts"]:
                publish = f"; master publish attempts {doc['publish_attempts']}, last: {doc['last_publish']}"
            logger.error(
                "ALERT bundle_missed: epoch %d has no consumable weight bundle after %d checks "
                "(last consume: %s%s); consecutive missed epochs: %d. Weights stay on the last valid bundle.",
                epoch, doc["bad_ticks"], doc["last_consume"], publish, doc["consecutive_missed"])
    _write(state_dir, doc)
    return doc


def summary(state_dir: str) -> dict:
    """The fields the weight-loop status file repeats."""
    d = read_health(state_dir)
    return {k: d.get(k) for k in ("epoch", "consumed", "missed", "consecutive_missed", "missed_total",
                                  "last_missed_epoch", "last_consumed_epoch", "last_consume",
                                  "last_publish", "updated_at")}
