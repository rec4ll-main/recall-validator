"""Strict typed boundary for the recall-allocation-bundle-v2 codec.

Frozen payload/envelope/binding value objects and the exact field validation shared
by the signer and the parser. No canonicalization or cryptography lives here.
"""

from __future__ import annotations

from dataclasses import dataclass

from bittensor_wallet.utils import get_ss58_format, is_valid_ss58_address

from score_version import (
    PROFILES,
    SUPPORTED_PROFILE,
    UnsupportedScoreVersion,
    require_supported_score_version,
)

SCHEMA = "recall-allocation-bundle-v2"
SS58_FORMAT = 42
MAX_BYTES = 1 << 20
MAX_ENTRIES = 4096
SCORE_VERSION, SCORE_KIND, EVIDENCE_WINDOW_SECONDS = SUPPORTED_PROFILE

_PAYLOAD_FIELDS = (
    "schema",
    "genesis_hash",
    "netuid",
    "master_hotkey",
    "epoch_start_block",
    "subnet_epoch_index",
    "selection_block",
    "anchor_block",
    "anchor_hash",
    "score_version",
    "score_kind",
    "evidence_window_seconds",
    "snapshot_digest",
    "uids",
    "hotkeys",
    "weights_u16",
)

_BINDING_FIELDS = (
    "genesis_hash",
    "netuid",
    "master_hotkey",
    "epoch_start_block",
    "subnet_epoch_index",
    "selection_block",
    "anchor_block",
    "anchor_hash",
    "snapshot_digest",
)

_HEX = frozenset("0123456789abcdef")


class BundleCodecError(Exception):
    """A parse, validation, signature, or binding failure."""


def _require_int(value: object, name: str, bits: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BundleCodecError(f"{name} must be an integer")
    if value < 0 or value >= (1 << bits):
        raise BundleCodecError(f"{name} out of range")
    return value


def _require_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise BundleCodecError(f"{name} must be 0x + 64 hex characters")
    if not all(c in _HEX for c in value[2:]):
        raise BundleCodecError(f"{name} must be lowercase hex")
    return value


def _require_ss58(value: object, name: str) -> str:
    if not isinstance(value, str) or not is_valid_ss58_address(value) or get_ss58_format(value) != SS58_FORMAT:
        raise BundleCodecError(f"{name} must be a valid format-{SS58_FORMAT} SS58 address")
    return value


def _require_signature(value: object) -> str:
    if not isinstance(value, str) or len(value) != 128:
        raise BundleCodecError("signature must be 128 lowercase hex characters")
    if not all(c in _HEX for c in value):
        raise BundleCodecError("signature must be lowercase hex")
    return value


@dataclass(frozen=True, slots=True)
class BundlePayload:
    genesis_hash: str
    netuid: int
    master_hotkey: str
    epoch_start_block: int
    subnet_epoch_index: int
    selection_block: int
    anchor_block: int
    anchor_hash: str
    score_version: str
    score_kind: str
    evidence_window_seconds: int
    snapshot_digest: str
    uids: tuple[int, ...]
    hotkeys: tuple[str, ...]
    weights_u16: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "genesis_hash": self.genesis_hash,
            "netuid": self.netuid,
            "master_hotkey": self.master_hotkey,
            "epoch_start_block": self.epoch_start_block,
            "subnet_epoch_index": self.subnet_epoch_index,
            "selection_block": self.selection_block,
            "anchor_block": self.anchor_block,
            "anchor_hash": self.anchor_hash,
            "score_version": self.score_version,
            "score_kind": self.score_kind,
            "evidence_window_seconds": self.evidence_window_seconds,
            "snapshot_digest": self.snapshot_digest,
            "uids": list(self.uids),
            "hotkeys": list(self.hotkeys),
            "weights_u16": list(self.weights_u16),
        }


@dataclass(frozen=True, slots=True)
class AllocationBundle:
    payload: BundlePayload
    signature: str


@dataclass(frozen=True, slots=True)
class ExpectedBindings:
    genesis_hash: str
    netuid: int
    master_hotkey: str
    epoch_start_block: int
    subnet_epoch_index: int
    selection_block: int
    anchor_block: int
    anchor_hash: str
    snapshot_digest: str
    cohort: tuple[tuple[int, str], ...]

    def __post_init__(self) -> None:
        _require_hash(self.genesis_hash, "genesis_hash")
        _require_int(self.netuid, "netuid", 16)
        _require_ss58(self.master_hotkey, "master_hotkey")
        _require_int(self.epoch_start_block, "epoch_start_block", 64)
        _require_int(self.subnet_epoch_index, "subnet_epoch_index", 64)
        _require_int(self.selection_block, "selection_block", 64)
        _require_int(self.anchor_block, "anchor_block", 64)
        _require_hash(self.anchor_hash, "anchor_hash")
        _require_hash(self.snapshot_digest, "snapshot_digest")
        if not isinstance(self.cohort, tuple):
            raise BundleCodecError("cohort must be a tuple")
        for uid, hotkey in self.cohort:
            _require_int(uid, "cohort uid", 16)
            _require_ss58(hotkey, "cohort hotkey")


def validate_payload(
    payload: object, *, require_supported_version: bool = True
) -> BundlePayload:
    """Validate a payload object.

    ``require_supported_version`` (default True) enforces the build's single supported
    score_version. Journal recovery of a pre-upgrade envelope may pass False so the
    structural fields (uids/weights) can be read; that path must never sign or accept
    a new submission.
    """
    if not isinstance(payload, dict):
        raise BundleCodecError("payload must be an object")
    if set(payload.keys()) != set(_PAYLOAD_FIELDS):
        raise BundleCodecError("unknown or missing payload fields")
    if not isinstance(payload["schema"], str) or payload["schema"] != SCHEMA:
        raise BundleCodecError("wrong schema")
    if require_supported_version:
        try:
            score_version = require_supported_score_version(
                payload["score_version"], "bundle"
            )
        except UnsupportedScoreVersion as exc:
            raise BundleCodecError(str(exc)) from exc
    else:
        raw_version = payload["score_version"]
        if not isinstance(raw_version, str) or not raw_version:
            raise BundleCodecError("score_version must be a non-empty string")
        score_version = raw_version
    # Strict path: the supported score_version 11 profile only. Journal recovery: any
    # KNOWN (kind, window) pair, so a persisted score_version 10 envelope from before the
    # upgrade still settles; it never signs or accepts new submissions.
    if require_supported_version:
        allowed = {SUPPORTED_PROFILE[1:]}
    else:
        allowed = {prof[1:] for prof in PROFILES.values()}
    score_kind = payload["score_kind"]
    if not isinstance(score_kind, str) or score_kind not in {k for k, _ in allowed}:
        raise BundleCodecError("wrong score kind")
    window = _require_int(payload["evidence_window_seconds"], "evidence_window_seconds", 32)
    if (score_kind, window) not in allowed:
        raise BundleCodecError("wrong evidence window")

    uids = payload["uids"]
    hotkeys = payload["hotkeys"]
    weights = payload["weights_u16"]
    if not isinstance(uids, list) or not isinstance(hotkeys, list) or not isinstance(weights, list):
        raise BundleCodecError("uids/hotkeys/weights_u16 must be lists")
    if not uids:
        raise BundleCodecError("vectors must be non-empty")
    if len(uids) > MAX_ENTRIES:
        raise BundleCodecError("too many entries")
    if len(hotkeys) != len(uids) or len(weights) != len(uids):
        raise BundleCodecError("vectors must be equal length")

    prev = -1
    seen_hotkeys: set[str] = set()
    out_uids: list[int] = []
    out_hotkeys: list[str] = []
    out_weights: list[int] = []
    for i in range(len(uids)):
        uid = _require_int(uids[i], "uid", 16)
        if uid <= prev:
            raise BundleCodecError("uids must be ascending and unique")
        prev = uid
        hotkey = _require_ss58(hotkeys[i], "hotkey")
        if hotkey in seen_hotkeys:
            raise BundleCodecError("duplicate hotkey")
        seen_hotkeys.add(hotkey)
        weight = _require_int(weights[i], "weight", 16)
        if weight <= 0:
            raise BundleCodecError("weights must be positive")
        out_uids.append(uid)
        out_hotkeys.append(hotkey)
        out_weights.append(weight)

    return BundlePayload(
        genesis_hash=_require_hash(payload["genesis_hash"], "genesis_hash"),
        netuid=_require_int(payload["netuid"], "netuid", 16),
        master_hotkey=_require_ss58(payload["master_hotkey"], "master_hotkey"),
        epoch_start_block=_require_int(payload["epoch_start_block"], "epoch_start_block", 64),
        subnet_epoch_index=_require_int(payload["subnet_epoch_index"], "subnet_epoch_index", 64),
        selection_block=_require_int(payload["selection_block"], "selection_block", 64),
        anchor_block=_require_int(payload["anchor_block"], "anchor_block", 64),
        anchor_hash=_require_hash(payload["anchor_hash"], "anchor_hash"),
        score_version=score_version,
        score_kind=score_kind,
        evidence_window_seconds=window,
        snapshot_digest=_require_hash(payload["snapshot_digest"], "snapshot_digest"),
        uids=tuple(out_uids),
        hotkeys=tuple(out_hotkeys),
        weights_u16=tuple(out_weights),
    )
