"""Failed or undispatched attempts never block later epochs.

Deterministic end-to-end runs of the real ``slave_consume`` + journal against the
chain/SDK fixture (``chain_v2_fake``), whose ``set_weights`` follows bittensor 10.5.0
control flow. "Restart" means the next tick reopens the SQLite journal from disk,
which turns any INTENT into UNKNOWN exactly like a process restart does.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import bundle_consume
from bundle_consume import slave_consume
from bundle_dispatch import EXPIRY_MARGIN_BLOCKS
from bundle_ipfs import IpfsError, compute_raw_cid
from bundle_journal import BundleJournal
from bundle_journal_types import ArtifactBinding, OperationKey, OperationKind
from bundle_store import artifact_digest
from chain_v2_fake import (
    GENESIS,
    HOTKEYS,
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


class World:
    """One chain, one slave state dir, and a content-addressed block store."""

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
        """Publish the master's bundle + pointer for the epoch starting at ``e``."""
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


E1, E2 = 3600, 3960  # tempo 360


# --------------------------------------------------------------------------- #
# Regression: the restart deadlock from the issue.
# --------------------------------------------------------------------------- #


def test_regression_sdk_rate_limit_no_dispatch_then_restart_recovers(tmp_path, monkeypatch):
    """SDK returns success=False without broadcasting (rate-limit guard), then restart.

    Before the fix this recorded UNKNOWN, and after the restart every later epoch was
    rejected with "cross-epoch unresolved operation blocks dispatch" forever.
    """
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    # Our precheck passes, then the chain moves before the SDK's own guard runs:
    # a weights write lands for this hotkey and the SDK loop never executes.
    real_precondition = bundle_consume.weights_send_precondition

    def racing_precondition(*a, **kw):
        out = real_precondition(*a, **kw)
        w.chain.weights_rows.setdefault(SLAVE_UID, []).append((w.chain.head, ((3, 1),)))
        return out

    monkeypatch.setattr(bundle_consume, "weights_send_precondition", racing_precondition)
    out = w.tick()
    monkeypatch.setattr(bundle_consume, "weights_send_precondition", real_precondition)

    assert out.status == "not_dispatched", out
    assert w.chain.submissions == []
    assert w.state_of(E1) == "not_dispatched"

    # Restart while still inside the rate-limit window: no INTENT is written.
    out = w.tick()
    assert out.status == "blocked" and "weights rate limit" in out.reason, out
    assert w.state_of(E1) == "not_dispatched"

    # A healthy tick after the window re-arms the same epoch and applies it once.
    w.chain.advance(w.chain.rate_limit + 1)
    out = w.tick()
    assert out.status == "applied", out
    assert w.state_of(E1) == "observed"
    assert len(w.applied()) == 1

    # Later epochs are not blocked.
    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    assert w.tick().status == "applied"
    assert len(w.applied()) == 2


def test_regression_legacy_unknown_row_no_longer_deadlocks_after_restart(tmp_path, monkeypatch):
    """The journal repro from the verification report: UNKNOWN at epoch N, restart,
    epoch N+1 is gated. Now the gate holds only until the mortality deadline."""
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 300)
    env = w.envelopes[E1]
    # Simulate a pre-fix UNKNOWN row (no deadline) left by an attempt that never sent.
    key = OperationKey(
        genesis_hash=GENESIS, netuid=NETUID, wallet_hotkey=SLAVE.ss58_address,
        epoch_start_block=E1, subnet_epoch_index=E1 // 360,
        operation_kind=OperationKind.WEIGHTS_SUBMIT,
    )
    from bundle_runtime import _journal_for
    from bundle_store import ArtifactStore

    ArtifactStore(str(w.state)).store(NETUID, E1 // 360, E1, env)
    j = _journal_for(str(w.state))
    j.prepare(key, ArtifactBinding(digest=artifact_digest(env), cid=compute_raw_cid(env)))
    j.begin(key, intent_block=E1 + 290)
    j.mark_unknown(key)
    j.close()

    # Restart into epoch N+1 before the deadline: still fenced (could still land).
    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out
    state, _, deadline = w.rows()[E1]
    # Stamped on first sight after the restart: that head bounds the signing block.
    assert state == "unknown"
    assert int(deadline) == E2 + 20 + EXPIRY_MARGIN_BLOCKS
    assert w.chain.submissions == []

    # After the deadline is finalized the old op is EXPIRED and N+1 proceeds.
    w.chain.advance_to(int(deadline))
    out = w.tick()
    assert out.status == "applied", out
    assert w.state_of(E1) == "expired"
    assert w.state_of(E2) == "observed"
    assert len(w.chain.submissions) == 1


# --------------------------------------------------------------------------- #
# Send preconditions before INTENT.
# --------------------------------------------------------------------------- #


def test_rate_limit_window_blocks_before_intent(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.weights_rows[SLAVE_UID] = [(E1 + 15, ((3, 1),))]  # set 5 blocks ago

    out = w.tick()

    assert out.status == "blocked"
    assert "5 blocks since last update <= rate limit 100" in out.reason
    assert w.state_of(E1) == "prepared"  # never INTENT
    assert w.sub.set_weights_calls == 0

    w.chain.advance(100)
    assert w.tick().status == "applied"


def test_unregistered_hotkey_blocks_before_intent(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    monkeypatch.setattr(w.sub, "get_uid_for_hotkey_on_subnet", lambda *a, **k: None)

    out = w.tick()

    assert out.status == "blocked" and "not registered" in out.reason
    assert w.sub.set_weights_calls == 0
    assert w.state_of(E1) == "prepared"


@pytest.mark.parametrize("read", ["commit_reveal_enabled", "get_current_block"])
def test_sdk_pre_send_rpc_error_is_not_dispatched_and_recovers(tmp_path, monkeypatch, read):
    """commit_reveal_enabled raises outside the SDK try; get_current_block inside it."""
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.failing_reads[read] = [ConnectionError("websocket closed")]

    out = w.tick()

    assert out.status == "not_dispatched", out
    assert "websocket closed" in out.reason
    assert w.chain.submissions == []
    assert w.state_of(E1) == "not_dispatched"

    out = w.tick()  # restart + healthy tick
    assert out.status == "applied", out
    assert len(w.applied()) == 1


# --------------------------------------------------------------------------- #
# Native max-weight cap on every row, the burn row included.
# --------------------------------------------------------------------------- #


def test_burn_row_over_native_cap_is_blocked_before_any_journal_row(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, native_cap=0.5)
    w.epoch(E1, with_burn=True)
    w.chain.advance_to(E1 + 20)
    monkeypatch.setattr(
        bundle_consume, "_signed_burn_uid",
        lambda *a, **k: (0, HOTKEYS[0]),
    )

    out = w.tick()

    assert out.status == "blocked", out
    assert out.reason.startswith("burn row uid 0 share 0.79")
    assert "exceeds native max-weight cap 0.500000" in out.reason
    assert w.rows() == {}
    assert w.sub.set_weights_calls == 0


def test_burn_row_within_native_cap_is_applied(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, native_cap=0.9)
    w.epoch(E1, with_burn=True)
    w.chain.advance_to(E1 + 20)
    monkeypatch.setattr(bundle_consume, "_signed_burn_uid", lambda *a, **k: (0, HOTKEYS[0]))

    assert w.tick().status == "applied"


# --------------------------------------------------------------------------- #
# Definitive rejection, timeouts, crashes.
# --------------------------------------------------------------------------- #


def test_definitive_rejection_is_terminal_and_later_epochs_proceed(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.submit_behaviors = ["fail"]

    out = w.tick()
    assert out.status == "failed", out
    assert "ExtrinsicFailed" in out.reason
    assert w.state_of(E1) == "failed"

    w.chain.advance(200)
    assert w.tick().status == "failed"  # never retried for the same epoch
    assert len(w.chain.submissions) == 1

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    assert w.tick().status == "applied"
    assert len(w.chain.submissions) == 2


def test_timeout_then_late_inclusion_applies_once_without_duplicate(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.submit_behaviors = ["timeout"]

    out = w.tick()
    assert out.status == "unknown", out
    state, _, deadline = w.rows()[E1]
    assert state == "unknown"
    assert int(deadline) == E1 + 20 + EXPIRY_MARGIN_BLOCKS

    w.chain.advance(3)
    out = w.tick()  # restart while the tx may still land: fenced, no resubmit
    assert out.status == "unknown" and "fenced until finalized block" in out.reason
    assert len(w.chain.submissions) == 1

    w.chain.include_pool()  # the timed-out transaction lands
    out = w.tick()
    assert out.status == "applied", out
    assert w.state_of(E1) == "observed"
    assert len(w.applied()) == 1
    assert len(w.chain.submissions) == 1


def test_timeout_then_proven_expiry_releases_the_gate(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 300)  # late in the epoch: deadline is past E2
    w.chain.submit_behaviors = ["drop"]
    assert w.tick().status == "unknown"
    deadline = int(w.rows()[E1][2])
    assert deadline > E2 + 20

    # Next epoch before the deadline: the old op could still land, so N+1 is gated.
    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out
    assert len(w.chain.submissions) == 1

    w.chain.advance_to(deadline)
    out = w.tick()
    assert w.state_of(E1) == "expired"
    assert out.status == "applied", out
    assert w.state_of(E2) == "observed"
    assert len(w.chain.submissions) == 2

    # EXPIRED is never retried for its own epoch.
    assert "expired: mortality deadline" in w.rows()[E1][1]


def test_crash_before_broadcast_recovers_after_deadline_without_submission(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.failing_reads["get_current_block"] = [SystemExit("killed before broadcast")]

    with pytest.raises(SystemExit):
        w.tick()
    assert w.state_of(E1) == "intent"
    assert w.chain.submissions == []

    out = w.tick()  # restart: INTENT -> UNKNOWN, deadline stamped, still fenced
    assert out.status == "unknown"
    deadline = int(w.rows()[E1][2])

    w.chain.advance_to(deadline)
    out = w.tick()
    assert out.status == "expired"
    assert w.chain.submissions == []

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    assert w.tick().status == "applied"


def test_crash_after_broadcast_before_recording_is_observed_once(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.submit_behaviors = ["crash"]

    with pytest.raises(SystemExit):
        w.tick()
    assert w.state_of(E1) == "intent"

    out = w.tick()  # restart reconciles the landed row
    assert out.status == "applied", out
    assert w.state_of(E1) == "observed"
    assert len(w.applied()) == 1
    assert len(w.chain.submissions) == 1


def test_crash_after_broadcast_of_a_pooled_tx_never_double_submits(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.submit_behaviors = ["timeout"]
    original = w.sub.substrate.submit_extrinsic

    def submit_then_die(ext, **kw):
        try:
            return original(ext, **kw)
        finally:
            raise SystemExit("killed after broadcast")

    monkeypatch.setattr(w.sub.substrate, "submit_extrinsic", submit_then_die)
    with pytest.raises(SystemExit):
        w.tick()
    monkeypatch.undo()
    monkeypatch.setattr(bundle_consume, "fetch_block", lambda api, cid, **kw: w.blocks[cid])
    monkeypatch.setattr(bundle_consume, "fetch_gateway", lambda api, cid, **kw: w.blocks[cid])

    for _ in range(3):
        w.chain.advance(1)
        assert w.tick().status == "unknown"
    w.chain.include_pool()
    assert w.tick().status == "applied"
    assert len(w.chain.submissions) == 1
    assert len(w.applied()) == 1


# --------------------------------------------------------------------------- #
# Commit-reveal.
# --------------------------------------------------------------------------- #


def test_commit_reveal_pending_then_revealed(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, commit_reveal=True)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)

    assert w.tick().status == "pending"
    assert w.state_of(E1) == "pending"
    w.chain.reveal_all()
    assert w.tick().status == "applied"


def test_failed_commit_reveal_does_not_block_later_epochs(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, commit_reveal=True)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    w.chain.submit_behaviors = ["fail"]
    assert w.tick().status == "failed"

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    assert w.tick().status == "pending"
    w.chain.reveal_all()
    assert w.tick().status == "applied"


def test_unrevealed_commit_expires_and_releases_the_gate(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, commit_reveal=True)
    w.epoch(E1)
    w.chain.advance_to(E1 + 20)
    assert w.tick().status == "pending"

    w.chain.drop_commits()  # reveal never happens
    out = w.tick()
    assert out.status == "unknown" and "fenced until" in out.reason
    deadline = int(w.rows()[E1][2])

    w.epoch(E2)
    w.chain.advance_to(max(E2 + 20, deadline))
    out = w.tick()
    assert w.state_of(E1) == "expired"
    assert out.status == "pending"


# --------------------------------------------------------------------------- #
# Cross-epoch timelocked commit fence (issue #14).
# --------------------------------------------------------------------------- #


def test_cross_epoch_timelocked_commit_fenced_past_mortality_until_reveal(
    tmp_path, monkeypatch
):
    """Timeout near epoch end; commit lands in N+1; fence holds past +256 until reveal."""
    w = World(tmp_path, monkeypatch, commit_reveal=True)
    w.epoch(E1)
    # 3958: still epoch N; mortality window (256) crosses into N+1 (starts 3960).
    w.chain.advance_to(E1 + 358)
    w.chain.submit_behaviors = ["timeout"]
    out = w.tick()
    assert out.status == "unknown", out
    deadline = int(w.rows()[E1][2])
    assert deadline == E1 + 358 + EXPIRY_MARGIN_BLOCKS
    assert E2 < deadline < E2 + 360  # before the N+2 reveal window in the fixture schedule

    # Include the pooled commit after the epoch boundary (stored under N+1).
    w.chain.advance_to(E2 + 2)
    w.chain.include_pool()
    assert any(
        who == SLAVE.ss58_address
        for who, _b, _ct, _r in w.chain.timelocked.get(E2 // 360, [])
    )
    assert not w.chain.timelocked.get(E1 // 360, [])

    # Publish N+1 so D can finalize; prior-op reconcile must see the N+1 pending commit.
    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out
    assert w.state_of(E1) == "pending", w.rows()[E1]
    assert len(w.chain.submissions) == 1

    # Advance past the original mortality deadline while the commit still awaits reveal.
    w.chain.advance_to(deadline)
    out = w.tick()
    assert w.state_of(E1) == "pending", w.rows()[E1]
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out
    assert len(w.chain.submissions) == 1

    w.chain.reveal_all()
    out = w.tick()
    assert w.state_of(E1) == "observed", w.rows()[E1]
    assert len(w.applied()) == 1
    # Spec475: reveal does not advance LastUpdate. The prior commit's rate window
    # has already elapsed, so N+1 may immediately commit once N is proven applied.
    assert len(w.chain.submissions) == 2
    assert out.status == "pending", out

    # N+1 remains pending without duplicating either operation.
    w.chain.advance(w.chain.rate_limit + 1)
    out = w.tick()
    assert out.status == "pending", out
    assert w.state_of(E2) == "pending"
    assert len(w.chain.submissions) == 2
    w.chain.reveal_all()
    assert w.tick().status == "applied"
    assert w.state_of(E2) == "observed"


def test_cross_epoch_dropped_commit_still_expires_after_deadline(tmp_path, monkeypatch):
    """A never-included (dropped) mortal commit-reveal attempt still releases after expiry."""
    w = World(tmp_path, monkeypatch, commit_reveal=True)
    w.epoch(E1)
    w.chain.advance_to(E1 + 358)
    w.chain.submit_behaviors = ["drop"]
    assert w.tick().status == "unknown"
    deadline = int(w.rows()[E1][2])

    w.epoch(E2)
    w.chain.advance_to(E2 + 20)
    out = w.tick()
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out

    w.chain.advance_to(deadline)
    out = w.tick()
    assert w.state_of(E1) == "expired"
    assert out.status == "pending", out
    assert len(w.chain.submissions) == 2


def test_cross_epoch_commit_scan_read_failure_stays_fenced(tmp_path, monkeypatch):
    """Unreadable candidate-epoch commit queues must not expire the op."""
    import bundle_chain

    w = World(tmp_path, monkeypatch, commit_reveal=True)
    w.epoch(E1)
    w.chain.advance_to(E1 + 358)
    w.chain.submit_behaviors = ["timeout"]
    assert w.tick().status == "unknown"
    deadline = int(w.rows()[E1][2])

    w.chain.advance_to(E2 + 2)
    w.chain.include_pool()
    w.epoch(E2)
    real_read = bundle_chain.read_timelocked_commits

    def flaky_read(subtensor, netuid, epoch, *, block_hash):
        if epoch == E2 // 360:
            raise bundle_chain.ChainError("timelocked commits read failed: pruned")
        return real_read(subtensor, netuid, epoch, block_hash=block_hash)

    monkeypatch.setattr(bundle_chain, "read_timelocked_commits", flaky_read)
    w.chain.advance_to(deadline)
    out = w.tick()
    # Fail closed: E1 stays nonterminal UNKNOWN (not EXPIRED), so N+1 stays gated.
    assert w.state_of(E1) == "unknown", w.rows()[E1]
    assert out.status == "blocked" and "cross-epoch unresolved" in out.reason, out


# --------------------------------------------------------------------------- #
# A later external APPLIED proof still reconciles.
# --------------------------------------------------------------------------- #


def test_later_external_applied_proof_supersedes_unknown(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.epoch(E1)
    w.chain.advance_to(E1 + 300)
    w.chain.submit_behaviors = ["drop"]
    assert w.tick().status == "unknown"
    deadline = int(w.rows()[E1][2])

    from allocation_encoding import convert_weights_and_uids

    _, distinct = convert_weights_and_uids([3, 4, 5, 6], [0.4, 0.2, 0.2, 0.2])
    env2 = w.epoch(E2, weights=distinct)
    w.chain.advance_to(E2 + 15)
    from bundle_v2 import parse_bundle

    p = parse_bundle(env2).payload
    w.chain.weights_rows.setdefault(SLAVE_UID, []).append(
        (E2 + 13, tuple(zip(p.uids, p.weights_u16)))
    )
    w.chain.advance_to(E2 + 20)
    assert w.chain.head < deadline

    w.tick()
    state, _, _ = w.rows()[E1]
    assert state == "observed"
    conn = sqlite3.connect(f"file:{w.state / 'journal.sqlite3'}?mode=ro", uri=True)
    ref = conn.execute(
        "SELECT observation_ref FROM operations WHERE epoch_start_block = ?", (str(E1),)
    ).fetchone()[0]
    conn.close()
    assert ref.startswith("superseded_by_epoch:11:applied:")
    assert len(w.chain.submissions) == 1
