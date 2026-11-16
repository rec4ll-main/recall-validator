"""Local durable operation journal (stdlib sqlite3, WAL + synchronous FULL).

Owns the state machine PREPARED -> INTENT -> PENDING/OBSERVED/UNKNOWN with a
single-winner atomic PREPARED->INTENT compare-and-set (BEGIN IMMEDIATE). A PENDING
operation carries a known on-chain commit that is awaiting reveal; it blocks
re-dispatch and advances to OBSERVED only on application-specific finalized proof
(WeightsSet plus exact row and identity), never LastUpdate alone. A
restarted INTENT is UNKNOWN and never auto-retried. Terminal failure states
(NOT_DISPATCHED, FAILED, EXPIRED) release the cross-epoch gate; only NOT_DISPATCHED
(nothing was broadcast) may be re-armed by ``begin``. EXPIRED requires a stamped
mortality deadline at or below the caller's finalized block. Storage is a local
operator-owned file (0600); missing/corrupt/drifted schemas, unexpected schema
objects, and invalid persisted row states fail closed and sqlite errors translate to
bounded journal errors. No secrets are stored; a supplied observation reference never
proves application.

Corruption/tampering is detected, not defended against a malicious OS admin.

Stale-restore fencing: after a backup restore, ``mark_restored`` / sidecar ``*.restore_fence`` blocks ``prepare``/``begin`` until quarantine or a safe clear with zero nonterminals — never a reset/retry API.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import assert_never

from bundle_journal_open import (
    clear_restore_fence_file,
    create_connection,
    is_restore_fenced,
    mark_journal_restored,
    open_connection,
    quarantine_journal,
)
from bundle_journal_query import JournalQueryMixin, assert_no_blocker
from bundle_journal_store import (
    KEY_WHERE,
    _encode_u64,
    key_params,
    now,
    parse_state,
    run_txn,
    validate_rows,
)
from bundle_journal_types import (
    DISPATCHABLE_STATES,
    NONTERMINAL_STATES,
    ArtifactBinding,
    DispatchAuthorization,
    JournalConflictError,
    JournalFenceError,
    JournalSchemaError,
    JournalState,
    JournalStateError,
    JournalStorageError,
    ObservationRef,
    OperationKey,
    OperationKind,
    OperationRecord,
    require_uint64,
)

__all__ = [
    "ArtifactBinding",
    "BundleJournal",
    "DispatchAuthorization",
    "JournalConflictError",
    "JournalFenceError",
    "JournalSchemaError",
    "JournalState",
    "JournalStateError",
    "JournalStorageError",
    "ObservationRef",
    "OperationKey",
    "OperationKind",
    "OperationRecord",
]


class BundleJournal(JournalQueryMixin):
    """Local durable operation journal with single-winner dispatch authorization."""

    def __init__(
        self, conn: sqlite3.Connection, path: Path, *, restore_fenced: bool = False
    ) -> None:
        self._conn = conn
        self._path = path
        self._restore_fenced = bool(restore_fenced)

    # -- construction ---------------------------------------------------- #

    @classmethod
    def create_new(cls, path: str | Path) -> "BundleJournal":
        p = Path(path)
        return cls(create_connection(p), p, restore_fenced=False)

    @classmethod
    def open_existing(cls, path: str | Path) -> "BundleJournal":
        p = Path(path)
        return cls(open_connection(p), p, restore_fenced=is_restore_fenced(p))

    # -- restore fence --------------------------------------------------- #

    @property
    def restore_fenced(self) -> bool:
        return self._restore_fenced

    def mark_restored(self, *, note: str = "restored") -> None:
        """Fence this open journal as restored; blocks prepare/begin."""
        mark_journal_restored(self._path, note=note)
        self._restore_fenced = True

    def clear_restore_fence(self) -> None:
        """Clear fence only when no INTENT/PENDING/UNKNOWN rows remain."""
        if not self._restore_fenced and not is_restore_fenced(self._path):
            return
        row = self._conn.execute(
            "SELECT 1 FROM operations WHERE state IN (?, ?, ?) LIMIT 1",
            (
                JournalState.INTENT.value,
                JournalState.PENDING.value,
                JournalState.UNKNOWN.value,
            ),
        ).fetchone()
        if row is not None:
            raise JournalFenceError(
                "cannot clear restore fence while nonterminal operations exist"
            )
        clear_restore_fence_file(self._path)
        self._restore_fenced = False

    def quarantine(self) -> Path:
        """Move this journal aside; closes connection. Returns quarantine path."""
        self._conn.close()
        dest = quarantine_journal(self._path)
        self._restore_fenced = False
        return dest

    def _require_unfenced(self) -> None:
        if self._restore_fenced or is_restore_fenced(self._path):
            self._restore_fenced = True
            raise JournalFenceError(
                "restore fence active; reconcile or quarantine required"
            )

    # -- state machine -------------------------------------------------- #

    def prepare(self, key: OperationKey, binding: ArtifactBinding) -> None:
        self._require_unfenced()
        def _do() -> None:
            validate_rows(self._conn)
            existing = self._conn.execute(
                f"SELECT state, artifact_digest, artifact_cid FROM operations "
                f"WHERE {KEY_WHERE}",
                key_params(key),
            ).fetchone()
            if existing is not None:
                state = parse_state(existing["state"])
                if state in NONTERMINAL_STATES:
                    raise JournalConflictError(
                        "unresolved operation blocks re-dispatch"
                    )
                if (existing["artifact_digest"], existing["artifact_cid"]) != (
                    binding.digest,
                    binding.cid,
                ):
                    raise JournalConflictError(
                        "conflicting artifact binding for existing operation"
                    )
                raise JournalConflictError("operation already prepared")
            assert_no_blocker(self._conn, key)
            ts = now()
            self._conn.execute(
                "INSERT INTO operations (genesis_hash, netuid, wallet_hotkey, "
                "epoch_start_block, subnet_epoch_index, operation_kind, "
                "artifact_digest, artifact_cid, state, observation_ref, "
                "intent_block, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                key_params(key)
                + (
                    binding.digest,
                    binding.cid,
                    JournalState.PREPARED.value,
                    ts,
                    ts,
                ),
            )

        run_txn(self._conn, _do)

    def begin(
        self,
        key: OperationKey,
        *,
        intent_block: int = 0,
        binding: ArtifactBinding | None = None,
    ) -> DispatchAuthorization | None:
        """Atomically authorize one dispatch: PREPARED/NOT_DISPATCHED -> INTENT.

        NOT_DISPATCHED is re-armable because nothing was broadcast. When ``binding``
        is given it must equal the persisted binding. Any other state returns None.
        """
        self._require_unfenced()
        intent_block = require_uint64(intent_block, "intent_block")

        def _do() -> DispatchAuthorization | None:
            validate_rows(self._conn)
            row = self._conn.execute(
                f"SELECT state, artifact_digest, artifact_cid FROM operations "
                f"WHERE {KEY_WHERE}",
                key_params(key),
            ).fetchone()
            if row is None:
                raise JournalStateError("no operation for key")
            state = parse_state(row["state"])
            if state not in DISPATCHABLE_STATES:
                return None
            if binding is not None and (
                row["artifact_digest"], row["artifact_cid"]
            ) != (binding.digest, binding.cid):
                raise JournalConflictError(
                    "conflicting artifact binding for existing operation"
                )
            assert_no_blocker(self._conn, key)
            self._conn.execute(
                f"UPDATE operations SET state = ?, intent_block = ?, outcome_ref = NULL, "
                f"expiry_deadline = NULL, updated_at = ? WHERE {KEY_WHERE} AND state = ?",
                (JournalState.INTENT.value, _encode_u64(intent_block), now())
                + key_params(key)
                + (state.value,),
            )
            return DispatchAuthorization(
                key=key,
                binding=ArtifactBinding(
                    digest=row["artifact_digest"], cid=row["artifact_cid"]
                ),
            )

        return run_txn(self._conn, _do)

    def observe(self, key: OperationKey, ref: ObservationRef) -> None:
        def _do() -> None:
            row = self._conn.execute(
                f"SELECT state, observation_ref FROM operations WHERE {KEY_WHERE}",
                key_params(key),
            ).fetchone()
            if row is None:
                raise JournalStateError("no operation for key")
            match parse_state(row["state"]):
                case JournalState.OBSERVED:
                    if row["observation_ref"] == ref.value:
                        return  # identical observation is idempotent
                    raise JournalConflictError("conflicting observation reference")
                case JournalState.PREPARED | JournalState.NOT_DISPATCHED:
                    raise JournalStateError("cannot observe an undispatched operation")
                case JournalState.FAILED | JournalState.EXPIRED:
                    raise JournalStateError("failed/expired operation is terminal")
                case JournalState.INTENT | JournalState.PENDING | JournalState.UNKNOWN:
                    self._conn.execute(
                        f"UPDATE operations SET state = ?, observation_ref = ?, "
                        f"updated_at = ? WHERE {KEY_WHERE}",
                        (JournalState.OBSERVED.value, ref.value, now())
                        + key_params(key),
                    )
                case unreachable:
                    assert_never(unreachable)

        run_txn(self._conn, _do)

    def observe_pending(self, key: OperationKey) -> None:
        """Record a non-terminal pending outcome (a commit made, reveal not observed).

        Transitions INTENT/PENDING/UNKNOWN to PENDING and never releases the gate or
        marks the operation applied. Only :meth:`observe` (with an exact finalized
        weights row) advances past PENDING to OBSERVED.
        """
        def _do() -> None:
            row = self._conn.execute(
                f"SELECT state FROM operations WHERE {KEY_WHERE}",
                key_params(key),
            ).fetchone()
            if row is None:
                raise JournalStateError("no operation for key")
            match parse_state(row["state"]):
                case JournalState.PENDING:
                    return  # already pending (idempotent)
                case JournalState.INTENT | JournalState.UNKNOWN:
                    self._conn.execute(
                        f"UPDATE operations SET state = ?, updated_at = ? "
                        f"WHERE {KEY_WHERE}",
                        (JournalState.PENDING.value, now()) + key_params(key),
                    )
                case JournalState.PREPARED | JournalState.NOT_DISPATCHED:
                    raise JournalStateError(
                        "cannot mark an undispatched operation pending"
                    )
                case JournalState.OBSERVED | JournalState.FAILED | JournalState.EXPIRED:
                    raise JournalStateError("terminal operation cannot become pending")
                case unreachable:
                    assert_never(unreachable)

        run_txn(self._conn, _do)

    def mark_unknown(self, key: OperationKey) -> None:
        def _do() -> None:
            row = self._conn.execute(
                f"SELECT state FROM operations WHERE {KEY_WHERE}",
                key_params(key),
            ).fetchone()
            if row is None:
                raise JournalStateError("no operation for key")
            match parse_state(row["state"]):
                case JournalState.INTENT | JournalState.PENDING:
                    self._conn.execute(
                        f"UPDATE operations SET state = ?, updated_at = ? "
                        f"WHERE {KEY_WHERE}",
                        (JournalState.UNKNOWN.value, now()) + key_params(key),
                    )
                case JournalState.UNKNOWN:
                    return  # already unknown (idempotent)
                case JournalState.PREPARED | JournalState.NOT_DISPATCHED:
                    raise JournalStateError(
                        "cannot mark an undispatched operation unknown"
                    )
                case JournalState.OBSERVED | JournalState.FAILED | JournalState.EXPIRED:
                    raise JournalStateError("terminal operation cannot become unknown")
                case unreachable:
                    assert_never(unreachable)

        run_txn(self._conn, _do)

    # -- terminal failure outcomes ------------------------------------- #

    def _set_outcome(
        self,
        key: OperationKey,
        target: JournalState,
        ref: ObservationRef,
        allowed: tuple[JournalState, ...],
        *,
        finalized_block: int | None = None,
    ) -> None:
        def _do() -> None:
            row = self._conn.execute(
                f"SELECT state, outcome_ref, expiry_deadline FROM operations "
                f"WHERE {KEY_WHERE}",
                key_params(key),
            ).fetchone()
            if row is None:
                raise JournalStateError("no operation for key")
            state = parse_state(row["state"])
            if state is target:
                if row["outcome_ref"] == ref.value:
                    return  # identical outcome is idempotent
                raise JournalConflictError("conflicting outcome reference")
            if state not in allowed:
                raise JournalStateError(
                    f"cannot move {state.value} operation to {target.value}"
                )
            if finalized_block is not None:
                deadline = row["expiry_deadline"]
                if deadline is None or int(deadline) > finalized_block:
                    raise JournalStateError(
                        "mortality deadline not reached at the finalized head"
                    )
            self._conn.execute(
                f"UPDATE operations SET state = ?, outcome_ref = ?, updated_at = ? "
                f"WHERE {KEY_WHERE}",
                (target.value, ref.value, now()) + key_params(key),
            )

        run_txn(self._conn, _do)

    def mark_not_dispatched(self, key: OperationKey, ref: ObservationRef) -> None:
        """INTENT -> NOT_DISPATCHED: the dispatch attempt never reached the submit RPC.

        Only the live attempt can prove this, so only INTENT is accepted (a restarted
        INTENT is already UNKNOWN). Releases the gate; ``begin`` may re-arm it.
        """
        self._set_outcome(
            key, JournalState.NOT_DISPATCHED, ref, (JournalState.INTENT,)
        )

    def mark_failed(self, key: OperationKey, ref: ObservationRef) -> None:
        """INTENT/PENDING/UNKNOWN -> FAILED on definitive failure evidence."""
        self._set_outcome(key, JournalState.FAILED, ref, NONTERMINAL_STATES)

    def mark_expired(
        self, key: OperationKey, ref: ObservationRef, *, finalized_block: int
    ) -> None:
        """INTENT/PENDING/UNKNOWN -> EXPIRED once the stamped deadline is finalized.

        Fails closed (JournalStateError) when no deadline is stamped or the deadline
        is above ``finalized_block``.
        """
        finalized_block = require_uint64(finalized_block, "finalized_block")
        self._set_outcome(
            key, JournalState.EXPIRED, ref, NONTERMINAL_STATES,
            finalized_block=finalized_block,
        )

    def stamp_expiry_deadline(self, key: OperationKey, deadline: int) -> int:
        """Persist the mortality deadline once; return the stored deadline.

        The first stamp wins (a later, larger bound never replaces it, and a smaller
        one is never accepted after the fact). Only non-terminal operations accept a
        stamp.
        """
        deadline = require_uint64(deadline, "deadline")

        def _do() -> int:
            row = self._conn.execute(
                f"SELECT state, expiry_deadline FROM operations WHERE {KEY_WHERE}",
                key_params(key),
            ).fetchone()
            if row is None:
                raise JournalStateError("no operation for key")
            if parse_state(row["state"]) not in NONTERMINAL_STATES:
                raise JournalStateError("only an unresolved operation takes a deadline")
            if row["expiry_deadline"] is not None:
                return int(row["expiry_deadline"])
            self._conn.execute(
                f"UPDATE operations SET expiry_deadline = ?, updated_at = ? "
                f"WHERE {KEY_WHERE}",
                (_encode_u64(deadline), now()) + key_params(key),
            )
            return deadline

        return run_txn(self._conn, _do)

    def close(self) -> None:
        self._conn.close()
