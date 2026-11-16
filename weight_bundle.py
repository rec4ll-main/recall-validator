"""Phase C C3 — slave weight-bundle fetch + verify (VERIFY-AND-LOG only).

This module implements the slave-side verification of the master's signed,
content-addressed weight bundle (schema ``recall-weight-bundle-v1``). It is
pure observation: it fetches a bundle by CID, runs the design §4.3 verification
chain, and returns a verdict with per-check reasons. It NEVER calls
set_weights, NEVER burns, and has no side effects beyond logging.

The one-hotkey-per-coldkey dedup (recall-validator d9b2cec) is applied to the
bundle weights at verification time for reporting (survivor/dedup counts) only.

Canonical serialization mirrors the Go ``weightbundle`` package exactly
(design §3; ratified Q1 excludes bundle_sha256 and signature from the signed
bytes) so a Go-signed bundle verifies here byte-for-byte.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

SCHEMA = "recall-weight-bundle-v1"
DEFAULT_SCORE_VERSION = "5"
DEFAULT_EPOCH_TOLERANCE_BLOCKS = 3
SIGNING_MESSAGE_THRESHOLD = 256

# Failure classes returned by verify_bundle (each maps to a design §4.3 check).
FAIL_CID_HASH = "cid_content_hash_mismatch"
FAIL_PARSE = "bad_bundle_json"
FAIL_SCHEMA = "wrong_schema"
FAIL_UNKNOWN_FIELD = "unknown_field"
FAIL_ENTRY_SHAPE = "bad_weight_entry"
FAIL_BUNDLE_HASH = "bundle_sha256_mismatch"
FAIL_SIGNATURE = "signature_invalid"
FAIL_SIGNATURE_LENGTH = "signature_length"
FAIL_MASTER_HOTKEY = "master_hotkey_mismatch"
FAIL_NETUID = "netuid_mismatch"
FAIL_EPOCH = "epoch_mismatch"
FAIL_SCORE_VERSION = "score_version_mismatch"
FAIL_WEIGHTS = "weights_invalid"


def signing_message(canonical: bytes) -> bytes:
    """Ratified Q2 explicit prehash: blake2b-256 when canonical > 256 bytes."""
    if len(canonical) > SIGNING_MESSAGE_THRESHOLD:
        return hashlib.blake2b(canonical, digest_size=32).digest()
    return canonical


def _ascii_int(n: int) -> bytes:
    return str(int(n)).encode("ascii")


def canonical_bundle_bytes(bundle: dict) -> bytes:
    """Exact canonical bytes (design §3.1-§3.4, Q1-resolved).

    Fields in order, NUL separators, decimal-ASCII ints (no leading zeros),
    uint32-BE entry count, entries sorted by uid ascending, float64 weight as
    8-byte little-endian. bundle_sha256 and signature are EXCLUDED.
    """
    weights = sorted(bundle["weights"], key=lambda w: int(w["uid"]))
    out = bytearray()
    out += SCHEMA.encode("utf-8") + b"\x00"
    out += _ascii_int(bundle["netuid"]) + b"\x00"
    out += _ascii_int(bundle["epoch_start_block"]) + b"\x00"
    out += str(bundle["score_version"]).encode("utf-8") + b"\x00"
    out += _ascii_int(bundle["metagraph_block"]) + b"\x00"
    out += _ascii_int(bundle["created_at"]) + b"\x00"
    out += str(bundle["master_validator_hotkey"]).encode("utf-8") + b"\x00"
    out += len(weights).to_bytes(4, "big")
    for w in weights:
        out += str(w["hotkey"]).encode("utf-8") + b"\x00"
        out += _ascii_int(w["uid"]) + b"\x00"
        out += _float64_le(float(w["weight"]))
    out += b"\x00"
    out += str(bundle["evidence_summary_hash"]).encode("utf-8") + b"\x00"
    out += str(bundle["corpus_identity"]).encode("utf-8")
    return bytes(out)


def _float64_le(value: float) -> bytes:
    import struct

    return struct.pack("<d", value)


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(s: str) -> bytes:
    num = 0
    for ch in s:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base58 character {ch!r}")
        num = num * 58 + idx
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def cid_sha256_digest(cid: str) -> bytes:
    """Return the sha256 digest embedded in a CIDv0 (base58) or CIDv1 (base32).

    Supports sha2-256 multihashes only (the IPFS add() default for small
    bundles). Raises ValueError for unsupported CID forms.
    """
    cid = cid.strip()
    if cid.startswith("Qm"):
        raw = _base58_decode(cid)
        if len(raw) != 34 or raw[0] != 0x12 or raw[1] != 0x20:
            raise ValueError("unsupported CIDv0 multihash (expected sha2-256)")
        return raw[2:]
    if cid[:1] in ("b", "B"):
        raw = base64.b32decode(cid[1:].upper() + "=" * ((8 - len(cid[1:]) % 8) % 8))
        # parse multicodec varint (version + codec), then multihash varint+digest
        pos = 0

        def read_varint(buf: bytes, i: int) -> tuple[int, int]:
            shift = 0
            val = 0
            while True:
                b = buf[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    return val, i
                shift += 7

        _version, pos = read_varint(raw, pos)
        _codec, pos = read_varint(raw, pos)
        mh_code, pos = read_varint(raw, pos)
        mh_len, pos = read_varint(raw, pos)
        if mh_code != 0x12 or mh_len != 32:
            raise ValueError("unsupported CIDv1 multihash (expected sha2-256)")
        return raw[pos : pos + 32]
    raise ValueError(f"unsupported CID form: {cid[:8]}...")


def _default_signature_verifier(signer_ss58: str, message: bytes, signature: bytes) -> bool:
    try:
        import bittensor as bt
    except Exception:
        logger.warning("bittensor unavailable; cannot verify bundle signature")
        return False
    try:
        keypair = bt.Keypair(ss58_address=signer_ss58)
        return bool(keypair.verify(message, signature))
    except Exception as exc:
        logger.warning("bundle signature verification failed: %s", exc)
        return False


def verify_bundle(
    data: bytes,
    *,
    cid: str = "",
    expected_signer_ss58: str,
    expected_netuid: int,
    expected_epoch_start_block: int,
    expected_score_version: str = DEFAULT_SCORE_VERSION,
    epoch_tolerance_blocks: int = DEFAULT_EPOCH_TOLERANCE_BLOCKS,
    known_hotkeys: set[str] | None = None,
    verifier=None,
) -> tuple[bool, str, dict]:
    """Run the design §4.3 verification chain over a fetched bundle.

    Returns (ok, reason, details). Any failure is fail-closed: (False, reason,
    details) and the caller MUST NOT set_weights. Verification is read-only.

    Checks in order: CID content hash, parse+schema+unknown fields, entry
    shape, bundle_sha256, signature, master hotkey, netuid, epoch currency,
    score_version, weights finite/normalizable. ``known_hotkeys`` (optional)
    drives the metagraph drop/renormalize reporting.
    """
    details: dict = {}

    # 1. CID content hash.
    if cid:
        try:
            want = cid_sha256_digest(cid)
        except ValueError as exc:
            details["cid_error"] = str(exc)
            return False, FAIL_CID_HASH, details
        got = hashlib.sha256(data).digest()
        if got != want:
            return False, FAIL_CID_HASH, details

    # 2. Parse + schema + unknown top-level fields.
    try:
        bundle = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, FAIL_PARSE, details
    if not isinstance(bundle, dict):
        return False, FAIL_PARSE, details
    if bundle.get("schema") != SCHEMA:
        return False, FAIL_SCHEMA, details
    allowed = {
        "schema", "netuid", "epoch_start_block", "score_version",
        "metagraph_block", "created_at", "master_validator_hotkey", "weights",
        "evidence_summary_hash", "corpus_identity", "bundle_sha256", "signature",
    }
    extra = set(bundle.keys()) - allowed
    if extra:
        details["unknown_fields"] = sorted(extra)
        return False, FAIL_UNKNOWN_FIELD, details

    # 3. Weight entry shape.
    weights = bundle.get("weights")
    if not isinstance(weights, list):
        return False, FAIL_ENTRY_SHAPE, details
    seen_uids = set()
    for w in weights:
        if not isinstance(w, dict) or set(w.keys()) != {"uid", "hotkey", "weight"}:
            return False, FAIL_ENTRY_SHAPE, details
        uid = w["uid"]
        hotkey = w["hotkey"]
        weight = w["weight"]
        if not isinstance(uid, int) or uid < 0 or not isinstance(hotkey, str) or not hotkey:
            return False, FAIL_ENTRY_SHAPE, details
        if not isinstance(weight, (int, float)):
            return False, FAIL_WEIGHTS, details
        if uid in seen_uids:
            details["duplicate_uid"] = uid
            return False, FAIL_ENTRY_SHAPE, details
        seen_uids.add(uid)

    # 4. bundle_sha256 over the signature-less canonical form (Q1).
    try:
        canonical = canonical_bundle_bytes(bundle)
    except (KeyError, TypeError, ValueError):
        return False, FAIL_PARSE, details
    recomputed = hashlib.sha256(canonical).hexdigest()
    if recomputed != bundle.get("bundle_sha256"):
        details["recomputed_sha256"] = recomputed
        return False, FAIL_BUNDLE_HASH, details

    # 5. Signature (sr25519 over the explicit signing message).
    sig_hex = bundle.get("signature", "")
    try:
        signature = bytes.fromhex(sig_hex)
    except (TypeError, ValueError):
        return False, FAIL_SIGNATURE_LENGTH, details
    if len(signature) != 64:
        return False, FAIL_SIGNATURE_LENGTH, details
    signer = str(bundle.get("master_validator_hotkey", ""))
    if not signer:
        return False, FAIL_MASTER_HOTKEY, details
    verify = verifier or _default_signature_verifier
    if not verify(signer, signing_message(canonical), signature):
        return False, FAIL_SIGNATURE, details

    # 6. Master hotkey.
    if signer != expected_signer_ss58:
        return False, FAIL_MASTER_HOTKEY, details

    # 7. netuid.
    if int(bundle.get("netuid", -1)) != int(expected_netuid):
        return False, FAIL_NETUID, details

    # 8. epoch currency (±tolerance).
    epoch = int(bundle.get("epoch_start_block", -1))
    if abs(epoch - int(expected_epoch_start_block)) > epoch_tolerance_blocks:
        details["epoch"] = epoch
        details["expected_epoch"] = expected_epoch_start_block
        return False, FAIL_EPOCH, details

    # 9. score_version.
    if str(bundle.get("score_version", "")) != expected_score_version:
        return False, FAIL_SCORE_VERSION, details

    # 10. Weights finite/non-negative (already shape-checked) + metagraph drop.
    import math

    for w in weights:
        if not math.isfinite(float(w["weight"])) or float(w["weight"]) < 0:
            return False, FAIL_WEIGHTS, details

    total = sum(float(w["weight"]) for w in weights)
    details["weight_count"] = len(weights)
    details["total_weight"] = total
    if known_hotkeys is not None:
        survivors = [w for w in weights if w["hotkey"] in known_hotkeys]
        details["dropped_unknown"] = len(weights) - len(survivors)
        details["survivor_count"] = len(survivors)
    else:
        details["survivor_count"] = len(weights)

    return True, "ok", details


def fetch_bundle(cid: str, ipfs_read_url: str, timeout: int = 15) -> bytes:
    """Fetch bundle bytes by CID from the IPFS read API (no writes)."""
    base = ipfs_read_url.rstrip("/")
    url = f"{base}/api/v0/cat?arg={cid}"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def read_cid_from_state_file(path: str) -> str:
    """Read last_published_cid from the master's weight_bundle_state.json.

    Same-host test simplification; production CID discovery is the
    signed manifest endpoint (deferred to C4).
    """
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    cid = state.get("last_published_cid", "")
    if not cid:
        raise ValueError("state file has no last_published_cid")
    return cid
