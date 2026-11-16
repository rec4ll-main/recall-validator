"""Pointer-publish guard: never spend Commitments space we cannot get back.

Root cause of a validator commitment stall (subtensor spec 477): ``Commitments.UsedSpaceOf``
resets only when ``SubnetEpochIndex`` changes. With the index frozen on a subnet the
master kept publishing a new CID every block-clock slot until ``used_space`` reached
``MaxSpace`` (3100), after which every ``set_commitment`` failed with
``SpaceLimitExceeded``.

Rules enforced before any IPFS or chain side effect of a pointer publish:

* Read ``CommitmentOf[netuid][hotkey]`` live; skip when it already holds this CID.
* Read ``MaxSpace``, ``UsedSpaceOf`` and ``SubnetEpochIndex`` live (never hardcoded).
  Effective used space is 0 when ``UsedSpaceOf.last_epoch`` != the live index.
  Charge at least 100 bytes per commitment; skip when the publish would not fit.
* Budget publishes per ``SubnetEpochIndex`` (default 1), persisted in the state dir,
  not per wall-clock hour or block-clock slot.
* A space shortfall or ``SpaceLimitExceeded`` while ``SubnetEpochIndex`` is frozen
  raises the distinct ``commitment_space_frozen_epoch`` alert.

The payload is already minimal: the raw CIDv1 string as a single Raw field.
Weights never depend on this path (see ``weight_loop``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("recall.bundle_commit_guard")

SPACE_LIMIT_ERROR = "SpaceLimitExceeded"
ALERT_FROZEN = "commitment_space_frozen_epoch"
ALERT_SPACE = "commitment_space_exhausted"
DEFAULT_BUDGET_PER_EPOCH_INDEX = 1
# Runtime: max(sum(field.len_for_rate_limit()), 100), applied once per commitment.
# https://github.com/RaoFoundation/subtensor/blob/5c6e83e2f4936f257234f246957f75684c35227c/pallets/commitments/src/lib.rs#L263-L270
MIN_COMMITMENT_SPACE = 100
_BUDGET_FILE = "pointer_publish_budget.json"


@dataclass(frozen=True, slots=True)
class CommitSpace:
    head: int
    epoch_index: int
    last_epoch_block: int | None
    tempo: int | None
    max_space: int
    used_space: int  # effective for the live epoch index
    current_cid: str | None

    @property
    def epoch_frozen(self) -> bool:
        if self.last_epoch_block is None or not self.tempo:
            return False
        return self.head - self.last_epoch_block > 2 * self.tempo


@dataclass(frozen=True, slots=True)
class Preflight:
    ok: bool
    status: str  # ok | unchanged | space | budget | error
    reason: str = ""
    space: CommitSpace | None = None


def _val(obj):
    return getattr(obj, "value", obj)


def _raw_cid(commitment) -> str | None:
    if not isinstance(commitment, dict):
        return None
    fields = (commitment.get("info") or {}).get("fields") or []
    if len(fields) != 1 or not isinstance(fields[0], dict) or len(fields[0]) != 1:
        return None
    ((_, value),) = fields[0].items()
    try:
        from bundle_chain import _decode_raw_bytes

        return _decode_raw_bytes(value).decode("utf-8")
    except Exception:
        return None


def read_commit_space(subtensor, netuid: int, hotkey: str) -> CommitSpace:
    sub = subtensor.substrate
    bh = sub.get_chain_head()
    head = int(sub.get_block_number(bh))

    def q(module, name, params):
        return _val(sub.query(module=module, storage_function=name, params=params, block_hash=bh))

    index = int(q("SubtensorModule", "SubnetEpochIndex", [netuid]) or 0)
    leb = q("SubtensorModule", "LastEpochBlock", [netuid])
    tempo = q("SubtensorModule", "Tempo", [netuid])
    max_space = int(q("Commitments", "MaxSpace", []) or 0)
    used = q("Commitments", "UsedSpaceOf", [netuid, hotkey]) or {}
    used_space = int(used.get("used_space", 0)) if int(used.get("last_epoch", -1)) == index else 0
    return CommitSpace(
        head=head, epoch_index=index,
        last_epoch_block=None if leb is None else int(leb),
        tempo=None if tempo is None else int(tempo),
        max_space=max_space, used_space=used_space,
        current_cid=_raw_cid(q("Commitments", "CommitmentOf", [netuid, hotkey])),
    )


def _budget_path(state_dir: str, netuid: int) -> str:
    return os.path.join(state_dir, f"{netuid}_{_BUDGET_FILE}")


def _load_budget(state_dir: str, netuid: int) -> dict:
    try:
        with open(_budget_path(state_dir, netuid)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_publish(state_dir: str, netuid: int, epoch_index: int) -> None:
    data = _load_budget(state_dir, netuid)
    count = int(data.get("count", 0)) if data.get("epoch_index") == epoch_index else 0
    os.makedirs(state_dir, exist_ok=True)
    tmp = _budget_path(state_dir, netuid) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"epoch_index": epoch_index, "count": count + 1}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _budget_path(state_dir, netuid))


def alert(code: str, detail: str) -> None:
    # Bittensor default-state initialization raises already-created third-party
    # loggers to CRITICAL. This dedicated logger emits ERROR safety alerts only;
    # restore its threshold at emission so import order cannot silence them.
    # Do not change root/SDK levels or enable unrelated third-party logging.
    logger.setLevel(logging.ERROR)
    logger.error("ALERT bundle_publish %s: %s", code, detail)


def preflight(
    subtensor, netuid: int, hotkey: str, cid: str, *, state_dir: str,
    budget_per_epoch_index: int = DEFAULT_BUDGET_PER_EPOCH_INDEX,
) -> Preflight:
    try:
        space = read_commit_space(subtensor, netuid, hotkey)
    except Exception as exc:
        return Preflight(False, "error", f"commitment space read failed: {exc}")
    if space.current_cid == cid:
        return Preflight(False, "unchanged", f"CommitmentOf already holds {cid}", space)
    # The pointer is one Raw field, whose rate-limit length is its UTF-8 byte count.
    cost = max(len(cid.encode("utf-8")), MIN_COMMITMENT_SPACE)
    if space.max_space and space.used_space + cost > space.max_space:
        detail = (f"used {space.used_space} + {cost} > MaxSpace {space.max_space} "
                  f"at SubnetEpochIndex {space.epoch_index}")
        if space.epoch_frozen:
            alert(ALERT_FROZEN, f"{detail}; index frozen since LastEpochBlock "
                  f"{space.last_epoch_block} (head {space.head}, tempo {space.tempo})")
        else:
            alert(ALERT_SPACE, detail)
        return Preflight(False, "space", detail, space)
    data = _load_budget(state_dir, netuid)
    used_budget = int(data.get("count", 0)) if data.get("epoch_index") == space.epoch_index else 0
    if used_budget >= budget_per_epoch_index:
        return Preflight(False, "budget",
                         f"publish budget {budget_per_epoch_index} spent for SubnetEpochIndex "
                         f"{space.epoch_index}", space)
    return Preflight(True, "ok", "", space)


def on_dispatch_failed(subtensor, netuid: int, hotkey: str, detail: str) -> None:
    """Distinct alert when the chain rejected for space while the epoch index is frozen."""
    if SPACE_LIMIT_ERROR not in (detail or ""):
        return
    try:
        space = read_commit_space(subtensor, netuid, hotkey)
    except Exception as exc:
        alert(ALERT_SPACE, f"{SPACE_LIMIT_ERROR}; space read failed: {exc}")
        return
    msg = (f"{SPACE_LIMIT_ERROR}: used {space.used_space}/{space.max_space} at "
           f"SubnetEpochIndex {space.epoch_index}, LastEpochBlock {space.last_epoch_block}, "
           f"head {space.head}")
    alert(ALERT_FROZEN if space.epoch_frozen else ALERT_SPACE, msg)
