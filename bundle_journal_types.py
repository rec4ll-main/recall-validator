"""Typed immutable public IDs, exhaustive states, and bounded typed failures.

This module owns the public value objects (OperationKey, ArtifactBinding,
ObservationRef, DispatchAuthorization), the exhaustive state/kind enums, and the
bounded typed exception set for the durable operation journal. Boundary validation
lives here: an invalid input is rejected at construction (ValueError), so the journal
interior never re-validates. No secrets are accepted or stored anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import scalecodec

U16_MAX = 65535
U64_MAX = 2**64 - 1
CID_MAX_BYTES = 128
OBSERVATION_MAX_BYTES = 512


class OperationKind(str, Enum):
    """Distinct journaled operation kinds."""

    POINTER_PUBLISH = "pointer_publish"
    WEIGHTS_SUBMIT = "weights_submit"


class JournalState(str, Enum):
    """Exhaustive states of a journaled operation.

    ``PENDING`` is non-terminal: a commit was made (e.g. commit-reveal weights) but its
    reveal is not yet observed on-chain. It blocks re-dispatch and is only ever
    advanced to ``OBSERVED`` by finalized WeightsSet plus exact row/identity proof,
    never by the commit or LastUpdate alone. ``OBSERVED`` is terminal and means
    the applied result was proven.

    Terminal failure states (each carries an ``outcome_ref``) release the cross-epoch
    gate because no transaction for the operation can still be included:

    - ``NOT_DISPATCHED``: the SDK returned or raised without ever calling the
      substrate submit (nothing was broadcast). This is the only re-armable state: the
      same epoch may dispatch again after a fresh precondition check.
    - ``FAILED``: definitive failure. The extrinsic was finalized with
      ``ExtrinsicFailed``, or (pointer) the exact finalized cutoff lacks the CID and
      no transaction for it can still land.
    - ``EXPIRED``: the mortality deadline of every possibly broadcast transaction is
      at or below the finalized head, so it can never be included. The application
      itself was not proven, so the same epoch is never retried.
    """

    PREPARED = "prepared"
    INTENT = "intent"
    PENDING = "pending"
    OBSERVED = "observed"
    UNKNOWN = "unknown"
    NOT_DISPATCHED = "not_dispatched"
    FAILED = "failed"
    EXPIRED = "expired"


NONTERMINAL_STATES = (JournalState.INTENT, JournalState.PENDING, JournalState.UNKNOWN)
# Terminal states that record a failure reason in ``outcome_ref``.
OUTCOME_STATES = (JournalState.NOT_DISPATCHED, JournalState.FAILED, JournalState.EXPIRED)
# States from which ``begin`` may authorize a (re-)dispatch of the same operation.
DISPATCHABLE_STATES = (JournalState.PREPARED, JournalState.NOT_DISPATCHED)


class JournalError(Exception):
    """Base class for all bounded journal failures."""


class JournalSchemaError(JournalError):
    """Database missing, corrupt, or schema/version mismatch (fails closed)."""


class JournalConflictError(JournalError):
    """Conflicting/duplicate intent or observation, or a cross-epoch block."""


class JournalStateError(JournalError):
    """Illegal state transition for the operation's current state."""


class JournalFenceError(JournalStateError):
    """Restore fence active: dispatch blocked until quarantine or safe clear."""


class JournalStorageError(JournalError):
    """Transient storage failure (locked database, I/O or commit error)."""


def _is_lower_hex64(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        return False
    return all(c in "0123456789abcdef" for c in value[2:])


def _is_uint(value: object, max_value: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= max_value
    )


def require_uint64(value: object, name: str) -> int:
    """Return ``value`` when it is an exact non-bool uint64, else raise ValueError.

    Used to validate the dispatch-intent block and query limits before any transaction
    is authorized, so a malformed input can never advance the persisted state.
    """
    if not _is_uint(value, U64_MAX):
        raise ValueError(f"{name} must be a uint64 int, not bool")
    return value


def _is_ss58(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and scalecodec.is_valid_ss58_address(value, 42)
    )


@dataclass(frozen=True, slots=True)
class OperationKey:
    """Immutable public identity of one journaled operation (the uniqueness key)."""

    genesis_hash: str
    netuid: int
    wallet_hotkey: str
    epoch_start_block: int
    subnet_epoch_index: int
    operation_kind: OperationKind

    def __post_init__(self) -> None:
        if not _is_lower_hex64(self.genesis_hash):
            raise ValueError("genesis_hash must be 0x + 64 lowercase hex chars")
        if not _is_uint(self.netuid, U16_MAX):
            raise ValueError("netuid must be a uint16 int, not bool")
        if not _is_ss58(self.wallet_hotkey):
            raise ValueError("wallet_hotkey must be a valid SS58 format-42 address")
        if not _is_uint(self.epoch_start_block, U64_MAX):
            raise ValueError("epoch_start_block must be a uint64 int, not bool")
        if not _is_uint(self.subnet_epoch_index, U64_MAX):
            raise ValueError("subnet_epoch_index must be a uint64 int, not bool")
        if not isinstance(self.operation_kind, OperationKind):
            raise ValueError("operation_kind must be an OperationKind")


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    """Immutable digest/CID binding for one intent (public artifact identity)."""

    digest: str
    cid: str

    def __post_init__(self) -> None:
        if not _is_lower_hex64(self.digest):
            raise ValueError("digest must be 0x + 64 lowercase hex chars")
        if (
            not isinstance(self.cid, str)
            or not self.cid
            or len(self.cid) > CID_MAX_BYTES
            or not self.cid.isascii()
        ):
            raise ValueError(f"cid must be nonempty ASCII <= {CID_MAX_BYTES} bytes")


@dataclass(frozen=True, slots=True)
class ObservationRef:
    """Caller-supplied public observation reference (never proves application)."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError("observation reference must be a string")
        try:
            nbytes = len(self.value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError(
                "observation reference must be valid Unicode (no surrogates)"
            ) from exc
        if nbytes == 0 or nbytes > OBSERVATION_MAX_BYTES:
            raise ValueError(
                f"observation reference must be nonempty and <= "
                f"{OBSERVATION_MAX_BYTES} bytes"
            )


@dataclass(frozen=True, slots=True)
class DispatchAuthorization:
    """One-time dispatch authorization returned only to the CAS winner."""

    key: OperationKey
    binding: ArtifactBinding


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """Read-only snapshot of one persisted operation for reconciliation.

    ``intent_block`` is the finalized block at which the dispatch intent was formed
    (``None`` for a not-yet-dispatched PREPARED operation). ``digest``/``cid`` are the
    persisted artifact binding used to reconstruct the expected outcome.
    ``expiry_deadline`` is a finalized block at or after which no transaction for this
    dispatch can still be included (``None`` until stamped). ``outcome_ref`` is the
    reason recorded with a terminal failure state.
    """

    key: OperationKey
    state: JournalState
    intent_block: int | None
    digest: str
    cid: str
    expiry_deadline: int | None = None
    outcome_ref: str | None = None
