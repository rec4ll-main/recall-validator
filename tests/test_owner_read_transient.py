"""A failed owner read blocks a secondary's consume for retry; it never rejects.

``resolve_owner_burn_uid`` used to fold a transient SubnetOwnerHotkey/Uids/Keys read
failure into "no owner", which hid the signed burn row, failed the row checks and
closed a valid publication for good (#36 made verification failures final). Real
``slave_consume`` against the chain/SDK fixture; only the substrate read is flaky.
"""

from __future__ import annotations

import sqlite3

import pytest

import bundle_consume
from chain_v2_fake import HOTKEYS, MINER_UIDS
from test_chain_v2_weights_lifecycle import E1, World

OWNER_READ_FAILED = "owner read failed; retrying"


def _world(tmp_path, monkeypatch, *, native_cap=0.9, **env_kw):
    w = World(tmp_path, monkeypatch, native_cap=native_cap)
    w.epoch(E1, with_burn=True, **env_kw)
    w.chain.advance_to(E1 + 20)
    w.rejected = []
    real_mark = bundle_consume.mark_rejected
    monkeypatch.setattr(bundle_consume, "mark_rejected",
                        lambda *a: w.rejected.append(a) or real_mark(*a))
    return w


def _flaky_owner_read(w, monkeypatch, storage="SubnetOwnerHotkey", fails=1):
    """Fail the next ``fails`` reads of ``storage`` (None: fail every read)."""
    real = w.sub.substrate.query
    left = [fails]

    def query(module, storage_function, params, block_hash=None):
        if storage_function == storage and (left[0] is None or left[0] > 0):
            if left[0] is not None:
                left[0] -= 1
            raise ConnectionError("transient")
        return real(module, storage_function, params, block_hash=block_hash)

    monkeypatch.setattr(w.sub.substrate, "query", query)
    return left


def _publication_states(w):
    path = w.state / "unavailable-bundle.sqlite3"
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        return [r[0] for r in db.execute("SELECT state FROM unavailable")]


def test_control_healthy_owner_read_applies(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    assert w.tick().status == "applied"


@pytest.mark.parametrize("storage", ["SubnetOwnerHotkey", "Uids", "Keys"])
def test_one_failed_owner_read_then_recovery_applies_on_next_poll(tmp_path, monkeypatch, storage):
    w = _world(tmp_path, monkeypatch)
    _flaky_owner_read(w, monkeypatch, storage=storage, fails=1)

    first = w.tick()
    assert (first.status, first.reason) == ("blocked", OWNER_READ_FAILED)
    assert w.rejected == [] and "rejected" not in _publication_states(w)
    assert w.rows() == {} and w.sub.set_weights_calls == 0

    w.chain.advance(10)
    second = w.tick()
    assert second.status == "applied", second
    assert len(w.applied()) == 1 and w.rejected == []


def test_persistent_owner_read_failure_stays_blocked_never_rejected(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    failing = _flaky_owner_read(w, monkeypatch, fails=None)

    # Polls well past the whole fetch-retry schedule: each one blocks for retry.
    for _ in range(6):
        out = w.tick()
        assert (out.status, out.reason) == ("blocked", OWNER_READ_FAILED)
        w.chain.advance(10)
    assert w.rejected == [] and "rejected" not in _publication_states(w)
    assert w.rows() == {} and w.sub.set_weights_calls == 0

    # The publication was never closed: once reads recover it is applied.
    failing[0] = 0
    assert w.tick().status == "applied"


def test_non_winner_row_with_successful_owner_read_is_rejected_for_good(tmp_path, monkeypatch):
    a, b = MINER_UIDS[0], MINER_UIDS[1]
    uids = [0] + list(MINER_UIDS)
    hotkeys = [HOTKEYS[u] for u in uids]
    hotkeys[1], hotkeys[2] = HOTKEYS[b], HOTKEYS[a]
    w = _world(tmp_path, monkeypatch, hotkeys=hotkeys)

    out = w.tick()
    assert (out.status, out.reason) == ("blocked", "payload uid is not a chain-dedup winner")
    assert len(w.rejected) == 1 and _publication_states(w) == ["rejected"]

    w.chain.advance(30)
    out = w.tick()
    assert out.status == "blocked" and "failed verification" in out.reason
    assert w.sub.set_weights_calls == 0


def test_over_cap_after_recovered_owner_read_is_rejected_for_good(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, native_cap=0.5)
    _flaky_owner_read(w, monkeypatch, fails=1)

    out = w.tick()
    assert (out.status, out.reason) == ("blocked", OWNER_READ_FAILED)
    assert w.rejected == []

    w.chain.advance(10)
    out = w.tick()
    assert out.status == "blocked"
    assert out.reason.startswith("burn row uid 0 share 0.79"), out
    assert len(w.rejected) == 1 and _publication_states(w) == ["rejected"]

    w.chain.advance(30)
    out = w.tick()
    assert out.status == "blocked" and "failed verification" in out.reason
    assert w.rows() == {} and w.sub.set_weights_calls == 0
