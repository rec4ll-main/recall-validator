"""Upgrade recovery for journal-bound historical v5/v7/v8/v9/v10 envelopes.

PR #17/#18 fixed fresh score_version 7 and v5 journal recovery. Later bumps advanced through
v8/v9/v10, and the score_version 11-only build (incentive v3 the sole scoring path) drops
v10 as well: an unresolved WEIGHTS_SUBMIT whose persisted envelope is still v5, v7, v8, v9
or v10 must still reach applied / pending / expired settlement under the v11 build,
without re-dispatching or blindly clearing the journal. Historical recovery is not
permission to ingest unsupported *new* versions.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import bundle_chain
import bundle_consume
from bundle_consume import slave_consume
from bundle_dispatch import EXPIRY_MARGIN_BLOCKS
from bundle_ipfs import IpfsError, compute_raw_cid
from bundle_journal_types import ArtifactBinding, OperationKey, OperationKind
from bundle_runtime import _journal_for
from bundle_store import ArtifactStore, artifact_digest
from bundle_v2 import BundleCodecError, parse_bundle, parse_bundle_for_journal_recovery
from chain_v2_fake import (
    GENESIS,
    HOTKEYS,
    MASTER,
    MINER_UIDS,
    NETUID,
    SLAVE,
    FakeChain,
    FakeMetagraph,
    FakeSubtensor,
    FakeWallet,
    build_envelope_for,
    config,
    publish_pointer,
)

SLAVE_UID = HOTKEYS.index(SLAVE.ss58_address)
E1, E2 = 3600, 3960  # tempo 360


class World:
    """One chain, one slave state dir, content-addressed IPFS blocks."""

    def __init__(self, tmp_path, monkeypatch, **chain_kw):
        self.chain = FakeChain(**chain_kw)
        self.sub = FakeSubtensor(self.chain)
        self.state = tmp_path / "state"
        self.blocks: dict[str, bytes] = {}
        self.envelopes: dict[int, bytes] = {}

        def fake_fetch(api_url, cid, **kw):
            data = self.blocks.get(cid)
            if data is None or compute_raw_cid(data) != cid:
                raise IpfsError("IPFS HTTP 404")
            return data

        monkeypatch.setattr(bundle_consume, "fetch_block", fake_fetch)
        monkeypatch.setattr(bundle_consume, "fetch_gateway", fake_fetch)

    def epoch(self, e: int, **env_kw) -> bytes:
        self.chain.advance_to(e + 5)
        env = build_envelope_for(self.chain, self.sub, e, **env_kw)
        self.blocks[publish_pointer(self.chain, env, e + 5)] = env
        self.envelopes[e] = env
        return env

    def tick(self):
        cfg = config(self.state, ipfs_api_url="http://127.0.0.1:5001")
        return slave_consume(self.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)

    def rows(self) -> dict[int, tuple]:
        path = Path(self.state / "journal.sqlite3")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return {
                int(r[0]): (r[1], r[2], r[3])
                for r in conn.execute(
                    "SELECT epoch_start_block, state, outcome_ref, expiry_deadline "
                    "FROM operations WHERE operation_kind = 'weights_submit'"
                )
            }
        finally:
            conn.close()

    def state_of(self, e: int) -> str:
        return self.rows()[e][0]

    def applied(self):
        return [(b, uid) for b, uid, _ in self.chain.applied_weight_sets if uid == SLAVE_UID]


def _legacy_envelope(chain, sub, e: int, *, score_version: str, weights=None) -> bytes:
    """Authentic bytes an older master would have signed; strict parse refuses them."""
    from allocation_encoding import convert_weights_and_uids
    from allocation_snapshot import build_snapshot, snapshot_digest
    from bundle_schedule import PUBLICATION_CUTOFF_BLOCKS
    from bundle_v2 import signing_message

    snap = build_snapshot(sub, FakeMetagraph(), NETUID, None, GENESIS, block=e)
    uids = list(MINER_UIDS)
    if weights is None:
        _, weights = convert_weights_and_uids(list(uids), [0.25] * len(uids))
    payload = {
        "schema": "recall-allocation-bundle-v2",
        "genesis_hash": GENESIS,
        "netuid": NETUID,
        "master_hotkey": MASTER.ss58_address,
        "epoch_start_block": e,
        "subnet_epoch_index": e // chain.tempo,
        "selection_block": e + PUBLICATION_CUTOFF_BLOCKS,
        "anchor_block": snap.anchor_block,
        "anchor_hash": snap.anchor_hash,
        "score_version": score_version,
        "score_kind": "window_quality_v1",
        "evidence_window_seconds": 3600,
        "snapshot_digest": snapshot_digest(snap),
        "uids": uids,
        "hotkeys": [HOTKEYS[u] for u in uids],
        "weights_u16": list(weights),
    }
    sig = MASTER.sign(signing_message(payload)).hex()
    return json.dumps(
        {"payload": payload, "signature": sig},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()


def _legacy_v5_envelope(chain, sub, e: int, *, weights=None) -> bytes:
    return _legacy_envelope(chain, sub, e, score_version="5", weights=weights)


def _legacy_v7_envelope(chain, sub, e: int, *, weights=None) -> bytes:
    return _legacy_envelope(chain, sub, e, score_version="7", weights=weights)


def _legacy_v8_envelope(chain, sub, e: int, *, weights=None) -> bytes:
    return _legacy_envelope(chain, sub, e, score_version="8", weights=weights)


def _legacy_v9_envelope(chain, sub, e: int, *, weights=None) -> bytes:
    return _legacy_envelope(chain, sub, e, score_version="9", weights=weights)


def _legacy_v10_envelope(chain, sub, e: int, *, weights=None) -> bytes:
    return _legacy_envelope(chain, sub, e, score_version="10", weights=weights)


def _seed_unresolved_historical(
    w: World,
    e: int,
    *,
    score_version: str,
    state: str,
    intent_block: int | None = None,
    stamp_deadline: bool = False,
    digest: str | None = None,
) -> bytes:
    """Persist an authentic historical envelope + non-terminal WEIGHTS_SUBMIT journal row."""
    w.chain.advance_to(e + 1)
    env = _legacy_envelope(w.chain, w.sub, e, score_version=score_version)
    ArtifactStore(str(w.state)).store(NETUID, e // 360, e, env)
    key = OperationKey(
        genesis_hash=GENESIS,
        netuid=NETUID,
        wallet_hotkey=SLAVE.ss58_address,
        epoch_start_block=e,
        subnet_epoch_index=e // 360,
        operation_kind=OperationKind.WEIGHTS_SUBMIT,
    )
    binding = ArtifactBinding(
        digest=digest if digest is not None else artifact_digest(env),
        cid=compute_raw_cid(env),
    )
    intent = intent_block if intent_block is not None else e + 20
    j = _journal_for(str(w.state))
    j.prepare(key, binding)
    j.begin(key, intent_block=intent)
    if state == "pending":
        j.observe_pending(key)
    elif state == "unknown":
        j.mark_unknown(key)
    elif state == "intent":
        pass
    else:
        raise AssertionError(state)
    if stamp_deadline:
        j.stamp_expiry_deadline(key, intent + EXPIRY_MARGIN_BLOCKS)
    j.close()
    return env


def _seed_unresolved_v5(w, e, **kw):
    return _seed_unresolved_historical(w, e, score_version="5", **kw)


def _seed_unresolved_v7(w, e, **kw):
    return _seed_unresolved_historical(w, e, score_version="7", **kw)


def _seed_unresolved_v8(w, e, **kw):
    return _seed_unresolved_historical(w, e, score_version="8", **kw)


def _seed_unresolved_v9(w, e, **kw):
    return _seed_unresolved_historical(w, e, score_version="9", **kw)


def _seed_unresolved_v10(w, e, **kw):
    return _seed_unresolved_historical(w, e, score_version="10", **kw)


def _expected_row(env: bytes) -> tuple[tuple[int, int], ...]:
    payload = parse_bundle_for_journal_recovery(env).payload
    return tuple(zip(payload.uids, payload.weights_u16))


def _apply_row(w: World, env: bytes, at_block: int) -> None:
    """Directly stamp the exact expected weights row as already applied on-chain."""
    row = _expected_row(env)
    w.chain.advance_to(at_block)
    w.chain.weights_rows.setdefault(SLAVE_UID, []).append((at_block, row))
    w.chain.applied_weight_sets.append((at_block, SLAVE_UID, row))


# --------------------------------------------------------------------------- #
# Codec: strict vs journal-recovery
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("score_version,builder", [
    ("5", _legacy_v5_envelope),
    ("7", _legacy_v7_envelope),
    ("8", _legacy_v8_envelope),
    ("9", _legacy_v9_envelope),
    ("10", _legacy_v10_envelope),
])
def test_strict_parse_rejects_historical_but_recovery_parse_accepts(
    tmp_path, monkeypatch, score_version, builder
):
    w = World(tmp_path, monkeypatch)
    w.chain.advance_to(E1 + 1)
    env = builder(w.chain, w.sub, E1)
    with pytest.raises(BundleCodecError, match=f"unsupported score version '{score_version}'"):
        parse_bundle(env)
    recovered = parse_bundle_for_journal_recovery(env).payload
    assert recovered.score_version == score_version
    assert recovered.uids == tuple(MINER_UIDS)


# --------------------------------------------------------------------------- #
# Seeded INTENT / PENDING / UNKNOWN + historical envelope under v11 recovery
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed_fn", [_seed_unresolved_v5, _seed_unresolved_v7, _seed_unresolved_v8, _seed_unresolved_v9, _seed_unresolved_v10], ids=["v5", "v7", "v8", "v9", "v10"])
@pytest.mark.parametrize("seed_state", ["intent", "pending", "unknown"])
def test_historical_journal_already_applied_observes_and_releases_gate(
    tmp_path, monkeypatch, seed_fn, seed_state
):
    w = World(tmp_path, monkeypatch)
    env = seed_fn(w, E1, state=seed_state, intent_block=E1 + 20)
    _apply_row(w, env, E1 + 25)

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    assert w.state_of(E1) == "observed", w.rows()[E1]
    assert out.status == "applied", out
    assert w.state_of(E2) == "observed"
    assert len(w.chain.submissions) == 1  # only the fresh v11 epoch


@pytest.mark.parametrize("seed_fn", [_seed_unresolved_v5, _seed_unresolved_v7, _seed_unresolved_v8, _seed_unresolved_v9, _seed_unresolved_v10], ids=["v5", "v7", "v8", "v9", "v10"])
def test_historical_journal_expired_after_mortality_releases_gate(tmp_path, monkeypatch, seed_fn):
    w = World(tmp_path, monkeypatch)
    # Late intent so the mortality window still covers early E2 (same shape as #8/#11).
    seed_fn(w, E1, state="unknown", intent_block=E1 + 290)

    w.epoch(E2)
    # First sight stamps the deadline from this head; still before it → fenced.
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out
    state, _, deadline = w.rows()[E1]
    assert state == "unknown"
    assert int(deadline) == E2 + 20 + EXPIRY_MARGIN_BLOCKS
    assert w.chain.submissions == []

    w.chain.advance_to(int(deadline))
    out = w.tick()
    assert w.state_of(E1) == "expired", w.rows()[E1]
    assert out.status == "applied", out
    assert w.state_of(E2) == "observed"
    assert len(w.chain.submissions) == 1


@pytest.mark.parametrize("seed_fn", [_seed_unresolved_v5, _seed_unresolved_v7, _seed_unresolved_v8, _seed_unresolved_v9, _seed_unresolved_v10], ids=["v5", "v7", "v8", "v9", "v10"])
def test_historical_journal_pending_reveal_keeps_gate(tmp_path, monkeypatch, seed_fn):
    w = World(tmp_path, monkeypatch, commit_reveal=True)
    env = seed_fn(w, E1, state="unknown", intent_block=E1 + 350)
    row = _expected_row(env)
    # Pending commit under a later inclusion epoch (cross-epoch fence).
    w.chain.advance_to(E2 + 2)
    w.chain.timelocked.setdefault(E2 // 360, []).append(
        (SLAVE.ss58_address, E1 + 350, "00", 1)
    )
    w.chain._pending_reveals = [(E2 // 360, SLAVE.ss58_address, SLAVE_UID, row)]

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out
    assert w.state_of(E1) == "pending", w.rows()[E1]
    assert w.chain.submissions == []

    # Past mortality: still pending until reveal.
    deadline = E1 + 350 + EXPIRY_MARGIN_BLOCKS
    w.chain.advance_to(deadline)
    out = w.tick()
    assert w.state_of(E1) == "pending"
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out

    w.chain.reveal_all()
    out = w.tick()
    assert w.state_of(E1) == "observed", w.rows()[E1]
    assert len(w.applied()) == 1
    # Gate released; current epoch may be rate-limited after the reveal write.
    assert out.status in ("applied", "blocked"), out
    w.chain.advance(w.chain.rate_limit + 1)
    out = w.tick()
    assert out.status in ("applied", "pending"), out
    assert w.state_of(E2) in ("observed", "pending")
    assert len(w.chain.submissions) == 1


@pytest.mark.parametrize("seed_fn", [_seed_unresolved_v5, _seed_unresolved_v7, _seed_unresolved_v8, _seed_unresolved_v9, _seed_unresolved_v10], ids=["v5", "v7", "v8", "v9", "v10"])
def test_historical_journal_unreadable_chain_stays_fenced(tmp_path, monkeypatch, seed_fn):
    w = World(tmp_path, monkeypatch, commit_reveal=True)
    seed_fn(w, E1, state="unknown", intent_block=E1 + 350, stamp_deadline=True)
    deadline = E1 + 350 + EXPIRY_MARGIN_BLOCKS

    w.chain.advance_to(E2 + 2)
    w.chain.timelocked.setdefault(E2 // 360, []).append(
        (SLAVE.ss58_address, E1 + 350, "00", 1)
    )
    w.epoch(E2)
    real_read = bundle_chain.read_timelocked_commits

    def flaky_read(*a, **kw):
        raise bundle_chain.ChainError("timelocked commits read failed: pruned")

    monkeypatch.setattr(bundle_chain, "read_timelocked_commits", flaky_read)
    w.chain.advance_to(deadline)
    out = w.tick()
    assert w.state_of(E1) == "unknown", w.rows()[E1]
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out
    assert w.chain.submissions == []

    # Readable again, commit gone: E1 expires and E2 may commit (pending under CR).
    monkeypatch.setattr(bundle_chain, "read_timelocked_commits", real_read)
    w.chain.drop_commits()
    out = w.tick()
    assert w.state_of(E1) == "expired"
    assert out.status == "pending", out
    assert w.state_of(E2) == "pending"
    assert len(w.chain.submissions) == 1
    w.chain.reveal_all()
    assert w.tick().status == "applied"
    assert w.state_of(E2) == "observed"


@pytest.mark.parametrize("seed_fn", [_seed_unresolved_v5, _seed_unresolved_v7, _seed_unresolved_v8, _seed_unresolved_v9, _seed_unresolved_v10], ids=["v5", "v7", "v8", "v9", "v10"])
def test_historical_digest_mismatch_does_not_observe_from_unbound_bytes(tmp_path, monkeypatch, seed_fn):
    w = World(tmp_path, monkeypatch)
    # Journal binds a different digest than the stored envelope bytes.
    env = seed_fn(
        w,
        E1,
        state="unknown",
        intent_block=E1 + 20,
        digest="0x" + "ab" * 32,
    )
    _apply_row(w, env, E1 + 25)  # chain has the row, but digest auth fails

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    # Must not OBSERVE from unbound bytes; stays fenced until expiry.
    assert w.state_of(E1) == "unknown", w.rows()[E1]
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out
    assert w.chain.submissions == []


@pytest.mark.parametrize("seed_fn", [_seed_unresolved_v5, _seed_unresolved_v7, _seed_unresolved_v8, _seed_unresolved_v9, _seed_unresolved_v10], ids=["v5", "v7", "v8", "v9", "v10"])
def test_subsequent_v11_submission_restart_idempotent_no_duplicate(tmp_path, monkeypatch, seed_fn):
    w = World(tmp_path, monkeypatch)
    env = seed_fn(w, E1, state="unknown", intent_block=E1 + 20)
    _apply_row(w, env, E1 + 25)

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    assert w.tick().status == "applied"
    assert w.state_of(E1) == "observed"
    assert w.state_of(E2) == "observed"
    assert len(w.chain.submissions) == 1

    # Restart (re-open journal): already OBSERVED; no second dispatch.
    out = w.tick()
    assert out.status == "applied" and out.reason == "already observed", out
    assert len(w.chain.submissions) == 1
    assert len(w.applied()) == 2  # seeded apply + one v11 submission row write path
