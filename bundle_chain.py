"""Thin, source-confirmed chain reads plus the strict commitment-CID decoder.

The CID pointer lives in ``Commitments.CommitmentOf`` — ``{deposit, block,
info: {fields: [{RawN: hex}]}}`` — which is a *different* store from
``SubtensorModule.TimelockedWeightCommits`` (the commit-reveal pending store, keyed
``(netuid, epoch)``). The timelock commits are read through the substrate directly
because the SDK helper keys them by a ``mechid`` storage index instead of the runtime
``(netuid, epoch)`` double-map. The commitment decoder is bounded and strict: exactly
one ``RawN`` field, hex-decoded (never the hex string re-encoded literally), with the
metadata ``block`` retained and validated inside ``[E, D]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from bundle_dispatch import EXPIRY_MARGIN_BLOCKS

CID_MAX_BYTES = 128
MAX_CIPHERTEXT_HEX = 4096
# Hard cap on TimelockedWeightCommits point queries when scanning inclusion epochs.
COMMIT_EPOCH_SCAN_MAX = 16
_HEX = frozenset("0123456789abcdefABCDEF")


class ChainError(Exception):
    """A missing, invalid, mismatched, or pruned chain read."""


class ChainUnavailableError(ChainError):
    """The chain state could not be read at all (future/pruned block or RPC failure).

    Distinct from a readable value that is malformed or does not match: absence of a
    readable value is never evidence about what the chain contains.
    """


@dataclass(frozen=True, slots=True)
class TimelockedCommit:
    """One pending commit-reveal weight commit (who, commit_block, ciphertext, round)."""

    who: str
    commit_block: int
    ciphertext: str
    reveal_round: int


def _uint(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChainError(f"{name} must be a non-negative integer")
    return value


def _decode_raw_bytes(value: object) -> bytes:
    """Decode a known SDK byte/hex shape to bytes (strictly, never re-encode a str)."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        text = value.removeprefix("0x")
        if len(text) % 2 == 1 or any(c not in _HEX for c in text):
            raise ChainError("commitment Raw field is not hex")
        try:
            return bytes.fromhex(text)
        except ValueError:
            raise ChainError("commitment Raw field is not valid hex") from None
    if isinstance(value, (list, tuple)):
        out = bytearray()
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255:
                raise ChainError("commitment Raw byte list out of range")
            out.append(item)
        return bytes(out)
    raise ChainError("unrecognized commitment Raw field value")


def decode_commitment_cid(
    metadata: object, *, epoch_start_block: int, cutoff_block: int
) -> str | None:
    """Return the committed CID, or None when no commitment exists at this block.

    ``metadata`` is the ``CommitmentOfResponse`` dict ``{deposit, block, info}``.
    Missing (None / "") means no commitment; a malformed shape fails closed. The
    ``info.fields`` must contain exactly one ``RawN`` entry whose value is the
    hex-encoded CID bytes, and ``metadata.block`` must lie within ``[E, D]``.
    """
    if metadata is None or metadata == "":
        return None
    if not isinstance(metadata, dict):
        raise ChainError("commitment metadata is not an object")
    block = metadata.get("block")
    info = metadata.get("info")
    if block is None or info is None:
        raise ChainError("commitment metadata missing block/info")
    block = _uint(block, "commitment block")
    if not (epoch_start_block <= block <= cutoff_block):
        raise ChainError(
            f"commitment block {block} outside [{epoch_start_block}, {cutoff_block}]"
        )
    if not isinstance(info, dict) or not isinstance(info.get("fields"), list):
        raise ChainError("commitment info missing fields")
    fields = info["fields"]
    if len(fields) != 1:
        raise ChainError("commitment must contain exactly one Raw field")
    field = fields[0]
    if not isinstance(field, dict) or len(field) != 1:
        raise ChainError("commitment field must be a single Raw entry")
    ((name, value),) = field.items()
    if not isinstance(name, str) or not name.startswith("Raw") or not name[3:].isdigit():
        raise ChainError("commitment field is not a Raw entry")
    raw = _decode_raw_bytes(value)
    if len(raw) == 0 or len(raw) > CID_MAX_BYTES:
        raise ChainError("commitment Raw field length out of bounds")
    try:
        cid = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ChainError("commitment Raw data is not UTF-8") from None
    if not cid or not cid.isascii() or len(cid) > CID_MAX_BYTES:
        raise ChainError("commitment CID is not bounded ASCII")
    return cid


def read_pointer_cid(
    subtensor,
    netuid: int,
    master_hotkey: str,
    *,
    epoch_start_block: int,
    cutoff_block: int,
    block: int,
) -> str | None:
    """Read the master pointer at exactly ``block``; fail closed when the hash is gone.

    The SDK ``get_commitment_metadata`` silently falls back to the live tip when a
    future block hash is unavailable, so the pointer read here resolves the block hash
    explicitly and fails closed when it is missing (a future or pruned block), then
    queries the ``Commitments.CommitmentOf`` storage point at that exact hash. A valid
    hash with no commitment yields ``None`` (actionable "no pointer at D"), never a
    re-read of the live head.
    """
    try:
        block_hash = subtensor.get_block_hash(block)
    except Exception as exc:
        raise ChainUnavailableError(f"block hash resolution failed: {exc}") from exc
    if not block_hash:
        raise ChainUnavailableError(f"block {block} hash unavailable (future or pruned)")
    try:
        result = subtensor.substrate.query(
            module="Commitments",
            storage_function="CommitmentOf",
            params=[netuid, master_hotkey],
            block_hash=block_hash,
        )
    except Exception as exc:
        raise ChainUnavailableError(f"commitment read failed: {exc}") from exc
    if result is None or result.value is None:
        return None
    return decode_commitment_cid(
        result.value, epoch_start_block=epoch_start_block, cutoff_block=cutoff_block
    )


def read_timelocked_commits(
    subtensor, netuid: int, epoch: int, *, block_hash: str
) -> tuple[TimelockedCommit, ...]:
    """Read pending commit-reveal commits at ``(netuid, epoch)`` via a point query.

    ``SubtensorModule.TimelockedWeightCommits`` is a fully-keyed double map; the sync
    facade's ``query_map`` rejects a two-key query, so ``substrate.query`` is used and
    its ``value`` (the ``VecDeque`` of 4-tuples) is decoded directly. An empty queue is
    a valid "no commits" result; a malformed shape or an RPC error fails closed. The
    ciphertext is never logged.
    """
    try:
        result = subtensor.substrate.query(
            module="SubtensorModule",
            storage_function="TimelockedWeightCommits",
            params=[netuid, epoch],
            block_hash=block_hash,
        )
    except Exception as exc:
        raise ChainError(f"timelocked commits read failed: {exc}") from exc
    queue = result.value
    if queue is None:
        return ()
    if not isinstance(queue, (list, tuple)):
        raise ChainError("timelocked commits value is not a queue")
    commits: list[TimelockedCommit] = []
    for entry in queue:
        if not isinstance(entry, (list, tuple)) or len(entry) != 4:
            raise ChainError("timelocked commit entry is not a 4-tuple")
        who, commit_block, ciphertext, reveal_round = entry
        if not isinstance(who, str) or not who:
            raise ChainError("timelocked commit who is not an SS58 string")
        if not isinstance(ciphertext, str) or len(ciphertext) > MAX_CIPHERTEXT_HEX:
            raise ChainError("timelocked commit ciphertext is not a bounded hex string")
        commits.append(
            TimelockedCommit(
                who=who,
                commit_block=_uint(commit_block, "commit block"),
                ciphertext=ciphertext,
                reveal_round=_uint(reveal_round, "reveal round"),
            )
        )
    return tuple(commits)


def read_weights_row(
    subtensor, netuid: int, uid: int, *, block: int
) -> tuple[tuple[int, int], ...]:
    """Return the exact ``(dest_uid, u16_value)`` row for ``uid``, or empty."""
    for row_uid, weights in subtensor.weights(netuid, block=block):
        if row_uid == uid:
            return tuple((_uint(u, "row uid"), _uint(v, "row weight")) for u, v in weights or [])
    return ()


def read_last_update_block(subtensor, netuid: int, uid: int, *, block: int) -> int:
    """Return the actual block at which ``uid`` last set weights, or fail closed."""
    blocks_since = subtensor.blocks_since_last_update(netuid, uid, block=block)
    if blocks_since is None:
        raise ChainError("LastUpdate unavailable for uid")
    if isinstance(blocks_since, bool) or not isinstance(blocks_since, int) or blocks_since < 0:
        raise ChainError("LastUpdate read is not a non-negative integer")
    return block - blocks_since


def get_uid(subtensor, hotkey: str, netuid: int, *, block: int) -> int | None:
    """Fresh UID for a hotkey at ``block`` (None when unregistered)."""
    uid = subtensor.get_uid_for_hotkey_on_subnet(hotkey, netuid, block=block)
    if uid is None:
        return None
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise ChainError("uid is not a non-negative integer")
    return uid


class WeightStatus(Enum):
    """A reconcile outcome: pending commit, applied row, or non-provable unknown."""

    PENDING = "pending"
    APPLIED = "applied"
    UNKNOWN = "unknown"


def commit_epoch_candidates(
    *,
    journal_epoch: int,
    tempo: int | None = None,
    current_epoch: int | None = None,
) -> tuple[int, ...]:
    """Epochs where a mortal timelocked commit for this journal op may still live.

    The runtime stores ``TimelockedWeightCommits`` under the *inclusion-time* epoch,
    which can be later than the journal epoch when the extrinsic crosses a boundary.
    Extrinsic mortality is independent of the commit's reveal lifetime, so the scan
    covers every epoch reachable within the mortality window and, when known, through
    the current schedule epoch (a still-pending commit stays keyed by its inclusion
    epoch long after that window).
    """
    if isinstance(journal_epoch, bool) or not isinstance(journal_epoch, int) or journal_epoch < 0:
        raise ChainError("journal epoch is not a non-negative integer")
    if tempo is not None:
        if isinstance(tempo, bool) or not isinstance(tempo, int) or tempo <= 0:
            raise ChainError("tempo is not a positive integer")
        span = (EXPIRY_MARGIN_BLOCKS + tempo - 1) // tempo
    else:
        # Without tempo, still cover a single boundary cross (the common case).
        span = 1
    end = journal_epoch + span
    if current_epoch is not None:
        if (
            isinstance(current_epoch, bool)
            or not isinstance(current_epoch, int)
            or current_epoch < 0
        ):
            raise ChainError("current epoch is not a non-negative integer")
        if current_epoch > end:
            end = current_epoch
    if end - journal_epoch > COMMIT_EPOCH_SCAN_MAX:
        end = journal_epoch + COMMIT_EPOCH_SCAN_MAX
    return tuple(range(journal_epoch, end + 1))


def wallet_has_pending_commit(
    subtensor,
    wallet_hotkey: str,
    netuid: int,
    *,
    epochs: tuple[int, ...],
    bound: int,
    head,
) -> bool:
    """Scan actual native epoch keys, never the application's bundle sequence.

    ``epochs`` remains accepted for call compatibility but cannot establish a
    complete pending scan. A read failure or >64 keys fails closed.
    """
    if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
        raise ChainError("commit bound is not a non-negative integer")
    try:
        entries = subtensor.substrate.query_map(
            module="SubtensorModule", storage_function="TimelockedWeightCommits",
            params=[netuid], block_hash=head.hash,
        )
        native_epochs = []
        for count, (epoch, _) in enumerate(entries):
            if count >= 64:
                raise ChainError("pending commit scan exceeds safety bound")
            native_epochs.append(_uint(getattr(epoch, "value", epoch), "native commit epoch"))
    except ChainError:
        raise
    except Exception as exc:
        raise ChainError("native pending commit enumeration unavailable") from exc
    for epoch in native_epochs:
        commits = read_timelocked_commits(
            subtensor, netuid, epoch, block_hash=head.hash
        )
        if any(c.who == wallet_hotkey and c.commit_block >= bound for c in commits):
            return True
    return False


def reconcile_weights(
    subtensor,
    wallet_hotkey: str,
    netuid: int,
    *,
    epoch_start_block: int,
    subnet_epoch_index: int,
    expected_row: tuple[tuple[int, int], ...],
    intent_block: int,
    head,
    commit_epochs: tuple[int, ...] | None = None,
) -> WeightStatus:
    """Pending wins; application needs exact row plus finalized WeightsSet proof.

    LastUpdate is rate-limit activity, including CR commit, not a reveal marker.
    Legacy journal recovery uses the same proof as the active durable weight loop.
    """
    from weight_application_proof import find_application_proof
    bound = intent_block if intent_block > 0 else epoch_start_block
    epochs = commit_epochs if commit_epochs is not None else (subnet_epoch_index,)
    if wallet_has_pending_commit(
        subtensor, wallet_hotkey, netuid, epochs=epochs, bound=0, head=head,
    ):
        return WeightStatus.PENDING
    uid = get_uid(subtensor, wallet_hotkey, netuid, block=head.block)
    if uid is not None:
        row = read_weights_row(subtensor, netuid, uid, block=head.block)
        updated = read_last_update_block(subtensor, netuid, uid, block=head.block)
        if (
            row
            and row == expected_row
            and updated >= bound
        ):
            proof, _ = find_application_proof(
                subtensor, netuid=netuid, mechid=0, hotkey=wallet_hotkey, uid=uid,
                expected_row=expected_row, intent_block=bound,
                last_update=updated, head=head,
            )
            if proof is not None:
                return WeightStatus.APPLIED
    return WeightStatus.UNKNOWN
