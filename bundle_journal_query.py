"""Read-only persisted-record queries for the operation journal.

Owns the SELECT logic and the row -> typed (OperationKey / OperationRecord)
construction for the read model. The state-machine write path and connection lifecycle
live in ``bundle_journal``. ``unresolved`` orders the canonical decimal TEXT epoch
columns by (length, lexical) — numerically correct for any uint64 without an integer
CAST overflow — and caps the scan with a validated 1..16 ``limit``.
"""

from __future__ import annotations

from bundle_journal_store import (
    KEY_WHERE,
    decode_u64,
    key_params,
    parse_block,
    parse_intent_block,
    parse_state,
)
from bundle_journal_types import (
    NONTERMINAL_STATES,
    JournalConflictError,
    JournalState,
    JournalStateError,
    OperationKey,
    OperationKind,
    OperationRecord,
)

UNRESOLVED_LIMIT_MAX = 16
_NONTERMINAL_VALUES = tuple(s.value for s in NONTERMINAL_STATES)
_UNRESOLVED_ORDER = (
    "ORDER BY LENGTH(epoch_start_block), epoch_start_block, "
    "LENGTH(subnet_epoch_index), subnet_epoch_index"
)


def assert_no_blocker(conn, key: OperationKey) -> None:
    """Raise JournalConflictError when a cross-epoch non-terminal operation exists.

    A wallet+kind must have no INTENT/PENDING/UNKNOWN operation before a new dispatch.
    """
    blocker = conn.execute(
        "SELECT 1 FROM operations WHERE wallet_hotkey = ? AND operation_kind = ? "
        f"AND state IN ({', '.join('?' * len(_NONTERMINAL_VALUES))})",
        (key.wallet_hotkey, key.operation_kind.value) + _NONTERMINAL_VALUES,
    ).fetchone()
    if blocker is not None:
        raise JournalConflictError("cross-epoch unresolved operation blocks dispatch")


class JournalQueryMixin:
    """Read-model methods mixed into :class:`BundleJournal` (requires ``self._conn``)."""

    def state(self, key: OperationKey) -> JournalState:
        row = self._conn.execute(
            f"SELECT state FROM operations WHERE {KEY_WHERE}", key_params(key)
        ).fetchone()
        if row is None:
            raise JournalStateError("no operation for key")
        return parse_state(row["state"])

    def record(self, key: OperationKey) -> OperationRecord | None:
        row = self._conn.execute(
            "SELECT state, artifact_digest, artifact_cid, intent_block, "
            f"expiry_deadline, outcome_ref FROM operations WHERE {KEY_WHERE}",
            key_params(key),
        ).fetchone()
        if row is None:
            return None
        return _record(key, row)

    def unresolved(
        self, wallet_hotkey: str, operation_kind: OperationKind, *, limit: int = UNRESOLVED_LIMIT_MAX
    ) -> list[OperationRecord]:
        """Bounded read-only list of non-terminal operations for a wallet + kind.

        ``limit`` must be a non-bool int in [1, 16] (rejected otherwise, so a negative or
        missing bound can never produce an unbounded scan). Records are ordered by
        (epoch_start_block, subnet_epoch_index) numerically.
        """
        limit = require_unresolved_limit(limit)
        rows = self._conn.execute(
            "SELECT genesis_hash, netuid, epoch_start_block, subnet_epoch_index, "
            "operation_kind, state, artifact_digest, artifact_cid, intent_block, "
            "expiry_deadline, outcome_ref "
            "FROM operations WHERE wallet_hotkey = ? AND operation_kind = ? "
            f"AND state IN ({', '.join('?' * len(_NONTERMINAL_VALUES))}) "
            f"{_UNRESOLVED_ORDER} LIMIT ?",
            (wallet_hotkey, operation_kind.value) + _NONTERMINAL_VALUES + (limit,),
        ).fetchall()
        records: list[OperationRecord] = []
        for row in rows:
            key = OperationKey(
                genesis_hash=row["genesis_hash"],
                netuid=row["netuid"],
                wallet_hotkey=wallet_hotkey,
                epoch_start_block=decode_u64(row["epoch_start_block"]),
                subnet_epoch_index=decode_u64(row["subnet_epoch_index"]),
                operation_kind=operation_kind,
            )
            records.append(_record(key, row))
        return records


def _record(key: OperationKey, row) -> OperationRecord:
    return OperationRecord(
        key=key,
        state=parse_state(row["state"]),
        intent_block=parse_intent_block(row["intent_block"]),
        digest=row["artifact_digest"],
        cid=row["artifact_cid"],
        expiry_deadline=parse_block(row["expiry_deadline"], "expiry_deadline"),
        outcome_ref=row["outcome_ref"],
    )


def require_unresolved_limit(value: object) -> int:
    """Validate the unresolved-query limit: a non-bool int in [1, 16]."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an int in [1, 16], not bool")
    if not (1 <= value <= UNRESOLVED_LIMIT_MAX):
        raise ValueError(f"limit must be in [1, {UNRESOLVED_LIMIT_MAX}]")
    return value
