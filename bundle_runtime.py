"""Shared types and journal/chain helpers for the chain-bundle runtime.

Holds the immutable config/outcome value objects, the explicit role constants, the
genesis guard, and the journal open/existing-state helpers (first-boot vs resume).
The publish and consume orchestration live in ``bundle_publish`` and ``bundle_consume``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bundle_journal import BundleJournal
from bundle_journal_types import (
    JournalState,
    JournalStateError,
    OperationKey,
    OperationKind,
)
from bundle_store import ensure_state_dir, is_initialized, write_marker


class BundleRuntimeError(Exception):
    """A publish/consume failure that must fail closed (no legacy fallback)."""


ROLE_MASTER = "master"
ROLE_SLAVE = "slave"


@dataclass(frozen=True, slots=True)
class BundleConfig:
    netuid: int
    genesis_hash: str
    master_hotkey: str
    state_dir: str
    role: str
    ipfs_rotation_enabled: bool = False
    ipfs_api_url: str = ""
    gateway_urls: tuple[str, ...] = ()
    api_url: str = ""
    internal_api_key: str = ""
    allow_plaintext: bool = False  # score/API-key transport only
    ipfs_allow_plaintext: bool = True  # IPFS block fetch; default True (self-hosted)
    # S-14a: hostnames allowed for plaintext when ipfs_allow_plaintext is True
    # (configured API/read/gateway hosts + optional IPFS_PLAINTEXT_HOSTS).
    ipfs_plaintext_hosts: tuple[str, ...] = ()
    # When True, consume only verifies + persists the bundle (and marks it the newest
    # verified bundle); the block-clock weight loop (weight_loop.py) is the single
    # set_weights submitter. False keeps the legacy once-per-slot dispatch in consume.
    weight_loop_enabled: bool = False
    # Incentive v3 (score_version 11), the only scoring path. Master allocation settings.
    v3_eligibility_scope: str = ""
    v3_readiness_split: float = 0.40
    v3_burn_unearned: bool = True
    v3_readiness_mode: str = "shares"


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    status: str
    cid: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ConsumeOutcome:
    status: str
    reason: str = ""


def _guard_genesis(subtensor, expected_genesis_hash: str) -> None:
    """Verify the chain genesis hash before any side-effecting I/O with authority."""
    try:
        actual = subtensor.get_block_hash(0)
    except Exception as exc:
        raise BundleRuntimeError(f"genesis read failed: {exc}") from exc
    if not expected_genesis_hash or actual != expected_genesis_hash:
        raise BundleRuntimeError(
            f"genesis hash mismatch (expected {expected_genesis_hash}, got {actual})"
        )


def _journal_for(state_dir: str) -> BundleJournal:
    """Open the journal with an explicit first-boot vs resume marker.

    First boot (no marker) creates the journal then stamps the marker; resume (marker
    present) requires the journal to still exist — a lost journal after the marker is
    a state-loss failure, never a silent recreation.
    """
    directory: Path = ensure_state_dir(state_dir)
    journal_path = directory / "journal.sqlite3"
    if is_initialized(state_dir):
        if not journal_path.is_file():
            raise BundleRuntimeError(
                "journal lost after initialization marker; refusing to recreate"
            )
        return BundleJournal.open_existing(str(journal_path))
    journal = (
        BundleJournal.open_existing(str(journal_path))
        if journal_path.is_file()
        else BundleJournal.create_new(str(journal_path))
    )
    write_marker(state_dir)
    return journal


def _existing_state(journal: BundleJournal, key: OperationKey) -> JournalState | None:
    try:
        return journal.state(key)
    except JournalStateError:
        return None


def _publish_key(cfg: BundleConfig, wallet_hotkey: str, schedule) -> OperationKey:
    return OperationKey(
        genesis_hash=cfg.genesis_hash,
        netuid=cfg.netuid,
        wallet_hotkey=wallet_hotkey,
        epoch_start_block=schedule.epoch_start_block,
        subnet_epoch_index=schedule.subnet_epoch_index,
        operation_kind=OperationKind.POINTER_PUBLISH,
    )


def _weights_key(cfg: BundleConfig, wallet_hotkey: str, schedule) -> OperationKey:
    return OperationKey(
        genesis_hash=cfg.genesis_hash,
        netuid=cfg.netuid,
        wallet_hotkey=wallet_hotkey,
        epoch_start_block=schedule.epoch_start_block,
        subnet_epoch_index=schedule.subnet_epoch_index,
        operation_kind=OperationKind.WEIGHTS_SUBMIT,
    )
