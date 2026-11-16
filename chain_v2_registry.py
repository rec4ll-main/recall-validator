"""Master-only metagraph registry refresh for the chain-v2 runtime.

The chain-v2 loop is the only validator runtime. After each bundle tick this
module refreshes the web-owned miner registry for the master role only, derived
from a freshly validated finalized ``AllocationSnapshot`` and the COMPLETE
UID/hotkey mapping (``enumerate(snapshot.hotkeys)`` minus the validator itself),
never the coldkey-deduplicated allocation ``cohort``. A slave does not call the web API.

A refresh is produced only after identity validation:
  * the wallet hotkey equals the configured master hotkey,
  * the metagraph is bound to the configured netuid (the real ``metagraph.netuid``, not the
    snapshot's self-declared copy),
  * genesis and the full mapping were validated by ``build_snapshot``,
  * the metagraph actually synced at the exact finalized block (``metagraph.block``),
  * the master is present in the finalized mapping.

Any failed/uncertain check yields ``None`` (no HTTP request) — never a guessed
authoritative empty. The web handler contract (route/auth, UID order, ``registered_at``
preservation) and ``main.publish_metagraph_sync`` are reused unchanged.
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass
from typing import Protocol

from allocation_snapshot import AllocationSnapshotError, build_snapshot

logger = logging.getLogger(__name__)


class _SubstrateLike(Protocol):
    def get_chain_finalised_head(self) -> str: ...

    def get_block_number(self, block_hash: str | None = None) -> int: ...


class SubtensorLike(Protocol):
    """Minimal chain surface used by ``build_snapshot``."""

    substrate: _SubstrateLike

    def get_block_hash(self, block: int) -> str | None: ...

    def max_weight_limit(self, netuid: int, block: int | None = None) -> object: ...


class MetagraphLike(Protocol):
    """Minimal metagraph surface: construction-time netuid, synced block, identity lists."""

    netuid: int
    block: object  # torch/numpy tensor or int; read via _metagraph_block_number
    hotkeys: list[str]
    coldkeys: list[str]
    block_at_registration: list[int]

    def sync(
        self, subtensor=None, block: int | None = None, lite: bool | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RegistrySync:
    """A validated master-only registry refresh, ready to publish.

    ``miners`` is the complete finalized UID/hotkey mapping minus the validator itself, in
    ascending chain UID order (duplicate coldkeys are all retained). ``authoritative_empty``
    is True only when the validated finalized mapping held no miner other than the
    validator, qualifying the empty sync under the web handler's fail-closed guard.
    """

    netuid: int
    metagraph_block: int
    miners: tuple[tuple[int, str], ...]
    authoritative_empty: bool
    # Each miner's coldkey from the same finalized snapshot, aligned with ``miners``. The API
    # uses it to recognise traffic from an account linked to the serving miner's own coldkey
    # (self-dealing earns no service credit). Empty when unknown.
    coldkeys: tuple[str, ...] = ()


def _strict_index(value: object) -> int | None:
    """Return ``value`` as an exact int via ``operator.index``, or None otherwise.

    ``operator.index`` accepts only integral types (int, numpy integers) and rejects bool,
    float, and str — so a fractional block or a stringified netuid/block fails closed
    instead of being silently truncated by ``int()``.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return operator.index(value)
    except TypeError:
        return None


def _metagraph_block_number(metagraph: MetagraphLike) -> int | None:
    """Return the block the metagraph synced at, or None when unreadable.

    The real SDK stores ``metagraph.block`` as a torch/numpy tensor (``.item()``); the test
    fake stores a plain int. A missing/unreadable/non-integral block is None, which fails
    closed.
    """
    block = getattr(metagraph, "block", None)
    if block is None:
        return None
    item = getattr(block, "item", None)
    if callable(item):
        block = item()
    return _strict_index(block)


def build_registry_sync(
    subtensor: SubtensorLike,
    metagraph: MetagraphLike,
    netuid: int,
    expected_genesis_hash: str,
    wallet_hotkey: str,
    configured_master_hotkey: str,
) -> RegistrySync | None:
    """Build a validated master-only registry refresh, or None when not publishable.

    Returns None (no HTTP request) when the wallet is not the configured master, the
    metagraph is bound to a different netuid, the finalized snapshot cannot be validated
    (wrong genesis, missing/invalid mapping, chain read failure), the metagraph did not
    actually sync at the finalized block, or the master is absent from the finalized
    mapping. A None result must never be published as an authoritative empty.
    """
    if not configured_master_hotkey or wallet_hotkey != configured_master_hotkey:
        logger.warning(
            "chain-v2 registry refresh skipped: wallet hotkey is not the configured master"
        )
        return None
    metagraph_netuid = _strict_index(getattr(metagraph, "netuid", None))
    if metagraph_netuid != netuid:
        logger.warning(
            "chain-v2 registry refresh skipped: metagraph netuid %r != configured %d",
            getattr(metagraph, "netuid", None),
            netuid,
        )
        return None
    try:
        snapshot = build_snapshot(
            subtensor, metagraph, netuid, None, expected_genesis_hash
        )
    except AllocationSnapshotError as exc:
        logger.warning("chain-v2 registry refresh skipped: %s", exc)
        return None
    if _metagraph_block_number(metagraph) != snapshot.anchor_block:
        logger.warning(
            "chain-v2 registry refresh skipped: metagraph did not sync at the finalized block"
        )
        return None
    if wallet_hotkey not in snapshot.hotkeys:
        logger.warning(
            "chain-v2 registry refresh skipped: master absent from finalized mapping"
        )
        return None
    miners = tuple(
        (uid, hotkey)
        for uid, hotkey in enumerate(snapshot.hotkeys)
        if hotkey != wallet_hotkey
    )
    coldkeys = tuple(snapshot.coldkeys[uid] for uid, _ in miners)
    return RegistrySync(
        netuid=snapshot.netuid,
        metagraph_block=snapshot.anchor_block,
        miners=miners,
        authoritative_empty=not miners,
        coldkeys=coldkeys,
    )
