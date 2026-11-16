"""Strict signed allocation-v2 artifact codec (recall-allocation-bundle-v2).

Canonical JSON is ASCII (ensure_ascii, sorted, compact, allow_nan=False) UTF-8; the
signature is sr25519 over b"recall-allocation-bundle-v2\\x00" + sha256(canonical).
Envelope is exactly {"payload": ..., "signature": ...}. Parsing rejects duplicate/
unknown keys, boolean integers, out-of-range numbers, non-finite constants, and
non-SS58 identities; the caller supplies exact typed bindings. No sum=65535
constraint and no epoch/evidence eligibility claim.
"""

from __future__ import annotations

import hashlib
import json

from bittensor_wallet import Keypair

from bundle_v2_types import (
    SCHEMA,
    SS58_FORMAT,
    MAX_BYTES,
    AllocationBundle,
    BundleCodecError,
    BundlePayload,
    ExpectedBindings,
    _BINDING_FIELDS,
    _require_signature,
    validate_payload,
)

__all__ = [
    "SCHEMA",
    "AllocationBundle",
    "BundleCodecError",
    "BundlePayload",
    "ExpectedBindings",
    "canonical_bytes",
    "signing_message",
    "sign_bundle",
    "parse_bundle",
    "parse_bundle_for_journal_recovery",
    "verify_bundle",
]

DOMAIN_SEPARATOR = b"recall-allocation-bundle-v2\x00"


def canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def signing_message(payload: dict[str, object]) -> bytes:
    return DOMAIN_SEPARATOR + hashlib.sha256(canonical_bytes(payload)).digest()


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise BundleCodecError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(value: str) -> None:
    raise BundleCodecError("non-finite JSON number is not allowed")


def sign_bundle(payload: dict[str, object], keypair: Keypair) -> bytes:
    bundle = validate_payload(payload)
    if keypair.ss58_address != bundle.master_hotkey:
        raise BundleCodecError("signer keypair does not match master_hotkey")
    canonical = bundle.to_dict()
    try:
        signature = keypair.sign(signing_message(canonical)).hex()
    except Exception as exc:
        raise BundleCodecError(f"signing failed: {exc}") from exc
    envelope = {"payload": canonical, "signature": signature}
    return json.dumps(
        envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _parse_envelope_object(raw: bytes) -> tuple[dict[str, object], str]:
    """Decode envelope bytes to (payload dict, signature hex); no payload validation."""
    if not isinstance(raw, bytes):
        raise BundleCodecError("raw must be bytes")
    if len(raw) > MAX_BYTES:
        raise BundleCodecError("bundle exceeds 1 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BundleCodecError("bundle is not UTF-8") from None
    try:
        obj: object = json.loads(
            text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except BundleCodecError:
        raise
    except RecursionError:
        raise BundleCodecError("bundle is too deeply nested") from None
    except (json.JSONDecodeError, ValueError) as exc:
        raise BundleCodecError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise BundleCodecError("envelope must be an object")
    if set(obj.keys()) != {"payload", "signature"}:
        raise BundleCodecError("envelope must contain exactly payload and signature")
    signature = _require_signature(obj["signature"])
    payload_obj = obj["payload"]
    if not isinstance(payload_obj, dict):
        raise BundleCodecError("payload must be an object")
    return payload_obj, signature


def parse_bundle(raw: bytes) -> AllocationBundle:
    """Parse and strictly validate an envelope for the supported score_version only."""
    payload_obj, signature = _parse_envelope_object(raw)
    payload = validate_payload(payload_obj)
    return AllocationBundle(payload=payload, signature=signature)


def parse_bundle_for_journal_recovery(raw: bytes) -> AllocationBundle:
    """Parse a journal-bound historical envelope, allowing an unsupported score_version.

    Used only when reconciling an already-journaled WEIGHTS_SUBMIT whose persisted
    bytes predate this build's supported version. Structural validation still runs;
    the score_version field is accepted as any non-empty string so uids/weights can
    be compared to chain state. Callers must authenticate the bytes against the
    journal artifact digest and must never use this path to sign or accept a new
    submission.
    """
    payload_obj, signature = _parse_envelope_object(raw)
    payload = validate_payload(payload_obj, require_supported_version=False)
    return AllocationBundle(payload=payload, signature=signature)


def _check_bindings(payload: BundlePayload, expected: ExpectedBindings) -> None:
    for name in _BINDING_FIELDS:
        if getattr(payload, name) != getattr(expected, name):
            raise BundleCodecError(f"{name} binding mismatch")
    if tuple(zip(payload.uids, payload.hotkeys)) != expected.cohort:
        raise BundleCodecError("cohort binding mismatch")


def verify_bundle(raw: bytes, *, expected: ExpectedBindings) -> AllocationBundle:
    bundle = parse_bundle(raw)
    _check_bindings(bundle.payload, expected)
    try:
        keypair = Keypair(ss58_address=expected.master_hotkey, ss58_format=SS58_FORMAT)
        ok = keypair.verify(signing_message(bundle.payload.to_dict()), bytes.fromhex(bundle.signature))
    except Exception as exc:
        raise BundleCodecError(f"signature verification failed: {exc}") from exc
    if not ok:
        raise BundleCodecError("signature verification failed")
    return bundle
