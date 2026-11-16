"""Failed pointer publishes never block later epochs.

Runs the real ``master_publish`` / ``master_reconcile_pointer`` + journal against the
chain/SDK fixture. The signed envelope is pre-stored (the publish path reuses it
byte-for-byte), and the Kubo publish/pin/read-back calls are stubbed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import bundle_publish
from bundle_dispatch import EXPIRY_MARGIN_BLOCKS
from bundle_ipfs import compute_raw_cid
from bundle_publish import master_publish, master_reconcile_pointer
from bundle_store import ArtifactStore
from chain_v2_fake import (
    MASTER,
    NETUID,
    FakeChain,
    FakeMetagraph,
    FakeSubtensor,
    FakeWallet,
    build_envelope_for,
    config,
)


class Master:
    def __init__(self, tmp_path, monkeypatch, **chain_kw):
        self.chain = FakeChain(**chain_kw)
        self.sub = FakeSubtensor(self.chain)
        self.state = tmp_path / "state"
        self.cfg = config(self.state, ipfs_api_url="http://127.0.0.1:5001", role="master")
        monkeypatch.setattr(bundle_publish, "publish_block", lambda url, data, **k: compute_raw_cid(data))
        monkeypatch.setattr(bundle_publish, "pin_block", lambda *a, **k: None)
        monkeypatch.setattr(bundle_publish, "read_back_block", lambda url, cid, data, **k: data)

    def prepare_epoch(self, e: int) -> str:
        self.chain.advance_to(e + 1)
        env = build_envelope_for(self.chain, self.sub, e)
        self.state.mkdir(parents=True, exist_ok=True)
        ArtifactStore(str(self.state)).store(NETUID, e // self.chain.tempo, e, env)
        return compute_raw_cid(env)

    def publish(self):
        return master_publish(self.sub, FakeMetagraph(), FakeWallet(MASTER), self.cfg)

    def reconcile(self):
        master_reconcile_pointer(self.sub, self.cfg, MASTER.ss58_address)

    def rows(self):
        conn = sqlite3.connect(f"file:{Path(self.state / 'journal.sqlite3')}?mode=ro", uri=True)
        try:
            return {
                int(r[0]): (r[1], r[2], r[3])
                for r in conn.execute(
                    "SELECT epoch_start_block, state, outcome_ref, expiry_deadline "
                    "FROM operations WHERE operation_kind = 'pointer_publish'"
                )
            }
        finally:
            conn.close()

    def pointer(self):
        return self.chain.pointer_at(MASTER.ss58_address, self.chain.head)


E1, E2 = 3600, 3960


def test_successful_publish_is_observed_at_cutoff(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    cid = m.prepare_epoch(E1)
    assert m.publish().status == "pending"
    m.chain.advance_to(E1 + 12)
    m.reconcile()
    assert m.rows()[E1][0] == "observed"
    assert m.pointer()[1] == cid


def test_not_dispatched_pointer_is_rearmed_inside_the_window(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.chain.failing_reads["publish_metadata"] = [ConnectionError("websocket closed")]

    out = m.publish()
    assert out.status == "not_dispatched", out
    assert m.chain.submissions == []

    assert m.publish().status == "pending"  # restart + healthy tick, same epoch
    m.chain.advance_to(E1 + 12)
    m.reconcile()
    assert m.rows()[E1][0] == "observed"


def test_definitively_rejected_pointer_is_failed_and_next_epoch_publishes(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.chain.submit_behaviors = ["fail"]

    out = m.publish()
    assert out.status == "failed", out
    assert m.publish().status == "failed"  # not retried
    assert len(m.chain.submissions) == 1

    cid2 = m.prepare_epoch(E2)
    assert m.publish().status == "pending"
    m.chain.advance_to(E2 + 12)
    m.reconcile()
    assert m.rows()[E2][0] == "observed"
    assert m.pointer()[1] == cid2


def test_absent_at_finalized_cutoff_is_failed_once_nothing_can_land(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.chain.submit_behaviors = ["drop"]
    assert m.publish().status == "unknown"
    deadline = int(m.rows()[E1][2])
    assert deadline == E1 + 1 + EXPIRY_MARGIN_BLOCKS

    m.chain.advance_to(E1 + 12)
    m.reconcile()  # readable D without the CID, but a late inclusion is still possible
    state, ref, _ = m.rows()[E1]
    assert state == "unknown"

    m.chain.advance_to(deadline)
    m.reconcile()
    state, ref, _ = m.rows()[E1]
    assert state == "failed"
    assert ref.startswith(f"failed: commitment at finalized cutoff {E1 + 12} absent")

    m.prepare_epoch(E2)
    assert m.publish().status == "pending"


def test_included_after_cutoff_is_failed_immediately(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.chain.advance_to(E1 + 11)
    m.chain.submit_behaviors = ["late"]  # lands at D + 1

    assert m.publish().status == "pending"
    assert m.chain.head == E1 + 12 + 1
    m.reconcile()
    state, ref, _ = m.rows()[E1]
    assert state == "failed"
    assert "submission was included after the cutoff" in ref


def test_late_inclusion_cannot_overwrite_a_later_pointer_unchecked(tmp_path, monkeypatch):
    """With a short tempo the next window opens before the old deadline: it is gated."""
    m = Master(tmp_path, monkeypatch, tempo=100)
    e1, e2 = 3600, 3700
    m.prepare_epoch(e1)
    m.chain.submit_behaviors = ["timeout"]
    assert m.publish().status == "unknown"
    deadline = int(m.rows()[e1][2])
    assert deadline > e2 + 12

    m.chain.advance_to(e1 + 12)
    m.reconcile()
    cid2 = m.prepare_epoch(e2)
    out = m.publish()
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out

    # The old transaction lands late (after its own D, inside the next window).
    m.chain.include_pool()
    m.reconcile()
    assert m.rows()[e1][0] == "unknown"  # still fenced until the deadline

    m.chain.advance_to(deadline)
    m.reconcile()
    assert m.rows()[e1][0] == "failed"
    next_epoch = m.chain.epoch_start(m.chain.head) + m.chain.tempo
    cid3 = m.prepare_epoch(next_epoch)
    assert m.publish().status == "pending"
    m.chain.advance(1)
    assert m.pointer()[1] == cid3
    assert cid2 != cid3


def test_pruned_cutoff_is_not_absence_and_only_expires_after_deadline(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.chain.submit_behaviors = ["drop"]
    assert m.publish().status == "unknown"
    deadline = int(m.rows()[E1][2])

    m.chain.advance_to(E1 + 40)
    m.chain.state_pruned_below = E1 + 30  # D is unreadable
    m.reconcile()
    assert m.rows()[E1][0] == "unknown"

    m.chain.advance_to(deadline)
    m.reconcile()
    state, ref, _ = m.rows()[E1]
    assert state == "expired"  # not "failed": absence was never proven
    assert "unreadable" in ref


def test_pending_pointer_with_pruned_cutoff_expires_after_deadline(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    assert m.publish().status == "pending"
    m.chain.state_pruned_below = E1 + 100
    m.chain.advance_to(E1 + 200)
    m.reconcile()
    assert m.rows()[E1][0] == "pending"
    m.chain.advance_to(int(m.rows()[E1][2]))
    m.reconcile()
    assert m.rows()[E1][0] == "expired"
