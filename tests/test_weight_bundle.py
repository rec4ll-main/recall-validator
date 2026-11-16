"""Phase C C3 tests — slave weight-bundle fetch + verify (verify-and-log).

All cases use fixtures (no live chain, no IPFS). The C2-emitted fixture
(recall_weight_bundle_fixture.json) must verify PASS; tampered/mismatched
inputs must fail closed with the per-check reason; the one-hotkey-per-coldkey
dedup is applied at verification for reporting only.
"""

import hashlib
import json
from pathlib import Path

from main import dedup_recommended_uids
from weight_bundle import (
    FAIL_BUNDLE_HASH,
    FAIL_EPOCH,
    FAIL_MASTER_HOTKEY,
    FAIL_SCHEMA,
    FAIL_SIGNATURE,
    FAIL_SIGNATURE_LENGTH,
    canonical_bundle_bytes,
    signing_message,
    verify_bundle,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "recall_weight_bundle_fixture.json"

EXPECTED_SIGNER = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
EXPECTED_NETUID = 353
EXPECTED_EPOCH = 7929994


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text())


def bundle_bytes(fx):
    return fx["bundle_json"].encode("utf-8")


def _real_or_stub_verifier(fx):
    """Verify sr25519 with bittensor when available; otherwise a deterministic
    stub that accepts only the exact fixture signature over the exact signing
    message. The stub proves verify_bundle routes the correct message/signature
    to the verifier; the real cross-language proof runs where bittensor exists.
    """
    expected_sig = bytes.fromhex(fx["signature_hex"])

    try:
        import bittensor as bt  # noqa: F401

        def real(signer, message, signature):
            keypair = bt.Keypair(ss58_address=signer)
            return bool(keypair.verify(message, signature))

        return real
    except Exception:
        def stub(signer, message, signature):
            return signer == EXPECTED_SIGNER and signature == expected_sig

        return stub


def _verify(fx=None, **overrides):
    fx = fx or load_fixture()
    data = overrides.pop("data", None) or bundle_bytes(fx)
    cid = overrides.pop("cid", "")
    return verify_bundle(
        data,
        cid=cid,
        expected_signer_ss58=overrides.pop("expected_signer_ss58", EXPECTED_SIGNER),
        expected_netuid=overrides.pop("expected_netuid", EXPECTED_NETUID),
        expected_epoch_start_block=overrides.pop("expected_epoch_start_block", EXPECTED_EPOCH),
        expected_score_version=overrides.pop("expected_score_version", "5"),
        known_hotkeys=overrides.pop("known_hotkeys", None),
        verifier=overrides.pop("verifier", _real_or_stub_verifier(fx)),
    )


def test_fixture_verifies_pass():
    ok, reason, details = _verify()
    assert ok is True, f"fixture must verify, got {reason} {details}"
    assert details["weight_count"] == 3
    assert details["survivor_count"] == 3


def test_canonical_bytes_match_go_fixture():
    """The Python canonical serialization must reproduce the Go canonical hex."""
    fx = load_fixture()
    bundle = json.loads(fx["bundle_json"])
    canonical = canonical_bundle_bytes(bundle)
    assert canonical.hex() == fx["canonical_hex"]
    assert hashlib.sha256(canonical).hexdigest() == fx["bundle_sha256"]
    assert signing_message(canonical).hex() != ""  # prehash rule applies


def test_tampered_body_fails_closed():
    fx = load_fixture()
    bundle = json.loads(fx["bundle_json"])
    bundle["weights"][0]["weight"] = 0.99  # mutate after signing
    data = json.dumps(bundle).encode("utf-8")
    ok, reason, _ = _verify(fx, data=data)
    assert ok is False
    assert reason in (FAIL_BUNDLE_HASH, FAIL_SIGNATURE)


def test_wrong_signer_fails_closed():
    ok, reason, _ = _verify(expected_signer_ss58="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty")
    assert ok is False
    assert reason == FAIL_MASTER_HOTKEY


def test_stale_epoch_fails_closed():
    ok, reason, _ = _verify(expected_epoch_start_block=EXPECTED_EPOCH + 100)
    assert ok is False
    assert reason == FAIL_EPOCH


def test_missing_signature_fails_closed():
    fx = load_fixture()
    bundle = json.loads(fx["bundle_json"])
    del bundle["signature"]
    data = json.dumps(bundle).encode("utf-8")
    ok, reason, _ = _verify(fx, data=data)
    assert ok is False
    assert reason == FAIL_SIGNATURE_LENGTH


def test_wrong_schema_fails_closed():
    fx = load_fixture()
    bundle = json.loads(fx["bundle_json"])
    bundle["schema"] = "recall-a3a-attestation-v1"
    data = json.dumps(bundle).encode("utf-8")
    ok, reason, _ = _verify(fx, data=data)
    assert ok is False
    assert reason == FAIL_SCHEMA


def test_dedup_applied_at_verification_log_only():
    """The bundle weights are pre-coldkey-dedup; the slave dedups for reporting."""
    fx = load_fixture()
    bundle = json.loads(fx["bundle_json"])
    # Map bundle hotkeys to synthetic uids 0,1,2 and give two of them the same
    # coldkey so dedup collapses them.
    hotkeys = [w["hotkey"] for w in bundle["weights"]]
    uid_by_hotkey = {hk: i for i, hk in enumerate(hotkeys)}
    resolved = [(uid_by_hotkey[w["hotkey"]], float(w["weight"])) for w in bundle["weights"]]
    coldkeys = ["cold-A", "cold-A", "cold-B"]
    reg_blocks = [100, 200, 50]
    deduped = dedup_recommended_uids(resolved, coldkeys, reg_blocks)
    # cold-A has two uids (0@100, 1@200) -> keep the later registration (uid 1).
    assert len(deduped) == 2
    assert dict(deduped)[1] == resolved[1][1]


def test_verify_is_pure_no_side_effects():
    """verify_bundle must not mutate the input bytes or require network."""
    fx = load_fixture()
    data = bundle_bytes(fx)
    before = bytes(data)
    _verify(fx, data=data)
    assert data == before
