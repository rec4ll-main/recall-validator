"""MEV Shield follows the SDK default (BT_MEV_PROTECTION); journal behaviour under it.

The validator no longer passes ``mev_protection``: the SDK default applies. With MEV
on, an inner rejection is only visible as ``success=False`` next to the wrapper's
successful receipt, so it is UNCERTAIN, fenced as UNKNOWN, and becomes EXPIRED once
its mortality deadline is finalized. This is accepted behaviour; these tests pin it.
"""

from __future__ import annotations

import sqlite3

import pytest
from bittensor.core import settings

import bundle_publish
from bundle_ipfs import compute_raw_cid
from bundle_publish import master_publish, master_reconcile_pointer
from bundle_store import ArtifactStore
from chain_v2_fake import (
    MASTER,
    NETUID,
    FakeMetagraph,
    FakeWallet,
    build_envelope_for,
    config,
)
from test_chain_v2_weights_lifecycle import E1, E2, World


@pytest.fixture
def mev_on(monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_MEV_PROTECTION", True)


@pytest.fixture
def mev_off(monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_MEV_PROTECTION", False)


@pytest.mark.parametrize("default", [False, True])
def test_set_weights_does_not_override_the_sdk_default(tmp_path, monkeypatch, default):
    monkeypatch.setattr(settings, "DEFAULT_MEV_PROTECTION", default)
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    assert w.tick().status == "applied"
    assert w.sub.mev_protection_passed == [False]
    assert w.sub.mev_used == [default]
    kinds = [x.kind for x in w.chain.submissions]
    assert kinds == (["mev_shield"] if default else ["weights"])


def test_mev_success_is_applied(tmp_path, monkeypatch, mev_on):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    assert w.tick().status == "applied"
    assert w.state_of(E1) == "observed"
    assert len(w.applied()) == 1


def test_mev_inner_rejection_is_fenced_then_expired_not_failed(tmp_path, monkeypatch, mev_on):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.mev_inner_behaviors = ["fail"]

    out = w.tick()
    # The wrapper's receipt succeeded, so the rejection is not a definitive FAILED.
    assert out.status == "unknown", out
    assert w.state_of(E1) == "unknown"
    assert w.applied() == []
    deadline = int(w.rows()[E1][2])

    # Same epoch: never resubmitted while fenced.
    w.chain.advance(10)
    assert w.tick().status == "unknown"
    assert w.sub.set_weights_calls == 1

    # Next epoch before the deadline would be gated; at/after it the gate opens.
    w.epoch(E2)
    w.chain.advance_to(max(E2 + 20, deadline))
    out = w.tick()
    assert w.state_of(E1) == "expired"
    assert "expired: mortality deadline" in w.rows()[E1][1]
    assert out.status == "applied", out
    assert w.state_of(E2) == "observed"
    assert w.sub.set_weights_calls == 2


def test_mev_inner_rejection_gates_next_epoch_until_deadline(tmp_path, monkeypatch, mev_on):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 300)  # late attempt: deadline falls after E2's dispatch
    w.chain.mev_inner_behaviors = ["fail"]
    assert w.tick().status == "unknown"
    deadline = int(w.rows()[E1][2])
    assert deadline > E2 + 20

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out

    w.chain.advance_to(deadline)
    assert w.tick().status == "applied"
    assert w.state_of(E1) == "expired"


def test_plain_rejection_is_still_failed_immediately(tmp_path, monkeypatch, mev_off):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.submit_behaviors = ["fail"]
    assert w.tick().status == "failed"


@pytest.mark.parametrize("default", [False, True])
def test_set_commitment_does_not_override_the_sdk_default(tmp_path, monkeypatch, default):
    monkeypatch.setattr(settings, "DEFAULT_MEV_PROTECTION", default)
    from chain_v2_fake import FakeChain, FakeSubtensor

    chain = FakeChain()
    sub = FakeSubtensor(chain)
    state = tmp_path / "state"
    cfg = config(state, ipfs_api_url="http://127.0.0.1:5001", role="master")
    monkeypatch.setattr(bundle_publish, "publish_block", lambda url, data, **k: compute_raw_cid(data))
    monkeypatch.setattr(bundle_publish, "pin_block", lambda *a, **k: None)
    monkeypatch.setattr(bundle_publish, "read_back_block", lambda url, cid, data, **k: data)
    chain.advance_to(E1 + 1)
    env = build_envelope_for(chain, sub, E1)
    state.mkdir(parents=True)
    ArtifactStore(str(state)).store(NETUID, E1 // chain.tempo, E1, env)

    assert master_publish(sub, FakeMetagraph(), FakeWallet(MASTER), cfg).status == "pending"
    assert sub.mev_protection_passed == [False]
    assert sub.mev_used == [default]
    assert chain.pointer_at(MASTER.ss58_address, chain.head)[1] == compute_raw_cid(env)
    chain.advance_to(E1 + 12)
    master_reconcile_pointer(sub, cfg, MASTER.ss58_address)
    conn = sqlite3.connect(f"file:{state / 'journal.sqlite3'}?mode=ro", uri=True)
    try:
        (row_state,) = conn.execute(
            "SELECT state FROM operations WHERE operation_kind = 'pointer_publish'"
        ).fetchone()
    finally:
        conn.close()
    assert row_state == "observed"
