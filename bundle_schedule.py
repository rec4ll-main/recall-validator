"""Bundle block clock, the finalized chain head, and the fixed cutoff.

E is the bundle ``as_of_block``: the start of the current tempo-sized slot on the
*finalized head's own block clock* (``head - head % tempo``), with ``tempo`` read live
from the chain at that head. The bundle sequence (``subnet_epoch_index`` on the wire,
kept for envelope/journal compatibility) is ``head // tempo``. The chain's native
epoch marker (``LastMechansimStepBlock`` / ``last_epoch_block``) is carried only as
telemetry (``chain_epoch_block``): the chain once stalled that marker for >7k blocks via
chain-side epoch deferral, and gating bundles on it stopped weights entirely. Master
and slaves derive the same E from the same finalized head clock, so pointer/consume
bindings still line up. The application publication cutoff is
``D = E + PUBLICATION_CUTOFF_BLOCKS`` (12 blocks), a fixed versioned policy, not a
native margin and not seconds. The next epoch is never predicted. Every decision
reads a *fresh* finalized head/height — a caller-supplied ``current_block`` is never
trusted as the chain truth, and an epoch start that is not yet finalized is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

PUBLICATION_CUTOFF_BLOCKS = 12


class ScheduleError(Exception):
    """A missing, invalid, or changed epoch schedule or finalized head."""


@dataclass(frozen=True, slots=True)
class FinalizedHead:
    """A single fresh finalized head: block number plus its hash."""

    block: int
    hash: str


@dataclass(frozen=True, slots=True)
class ScheduleState:
    """Epoch facts pinned to one finalized head, plus the fixed cutoff."""

    epoch_start_block: int
    subnet_epoch_index: int
    tempo: int
    pending_epoch_at: int
    cutoff_block: int
    finalized_block: int
    finalized_hash: str
    # Telemetry only: the chain's native epoch marker. Never a gate.
    chain_epoch_block: int = 0
    chain_epoch_index: int = 0

    @property
    def as_of_block(self) -> int:
        return self.epoch_start_block

    @property
    def bundle_seq(self) -> int:
        return self.subnet_epoch_index

    def in_publish_window(self, block: int) -> bool:
        """True when ``block`` lies in the half-open publication window [E, D)."""
        return self.epoch_start_block <= block < self.cutoff_block

    def cutoff_finalized(self, block: int) -> bool:
        """True when ``block`` is at or past the cutoff D."""
        return block >= self.cutoff_block


def _uint(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScheduleError(f"{name} must be a non-negative integer")
    return value


def finalized_head(subtensor) -> FinalizedHead:
    """Read the current finalized head and its height from the substrate.

    The unfinalized ``get_current_block`` (chain head) is never used as a decision
    input; only the finalized head is. Any failure to read it fails closed.
    """
    try:
        head_hash = subtensor.substrate.get_chain_finalised_head()
        head_block = _uint(
            subtensor.substrate.get_block_number(head_hash), "finalized block number"
        )
    except ScheduleError:
        raise
    except Exception as exc:
        raise ScheduleError(f"finalized head unavailable: {exc}") from exc
    if not isinstance(head_hash, str) or not head_hash.startswith("0x"):
        raise ScheduleError("finalized head hash is not a 0x-prefixed string")
    return FinalizedHead(block=head_block, hash=head_hash)


def read_schedule(subtensor, netuid: int, *, head: FinalizedHead | None = None) -> ScheduleState:
    """Read the epoch schedule at a fresh finalized head (fetched when not given).

    Tempo is read *at* the finalized height; E and the bundle sequence come from the
    head's block clock, never from the chain epoch marker (telemetry only).
    """
    head = head if head is not None else finalized_head(subtensor)
    try:
        state = subtensor.get_epoch_schedule_state(netuid, block=head.block)
    except Exception as exc:
        raise ScheduleError(f"epoch schedule read failed: {exc}") from exc
    chain_e = _uint(state.last_epoch_block, "last_epoch_block")
    chain_index = _uint(state.subnet_epoch_index, "subnet_epoch_index")
    tempo = _uint(state.tempo, "tempo")
    _uint(state.pending_epoch_at, "pending_epoch_at")
    if tempo <= 0:
        raise ScheduleError("tempo must be positive")
    e, index = block_clock_slot(head.block, tempo)
    return ScheduleState(
        epoch_start_block=e,
        subnet_epoch_index=index,
        tempo=tempo,
        pending_epoch_at=e + tempo,
        cutoff_block=e + PUBLICATION_CUTOFF_BLOCKS,
        finalized_block=head.block,
        finalized_hash=head.hash,
        chain_epoch_block=chain_e,
        chain_epoch_index=chain_index,
    )


def block_clock_slot(head_block: int, tempo: int) -> tuple[int, int]:
    """(as_of_block, bundle_seq) for a finalized head on the tempo block clock."""
    if tempo <= 0:
        raise ScheduleError("tempo must be positive")
    return head_block - (head_block % tempo), head_block // tempo


def same_epoch(a: ScheduleState, b: ScheduleState) -> bool:
    """True when two schedules describe the same epoch (head advance is ignored)."""
    return (
        a.epoch_start_block == b.epoch_start_block
        and a.subnet_epoch_index == b.subnet_epoch_index
        and a.tempo == b.tempo
        and a.pending_epoch_at == b.pending_epoch_at
    )
