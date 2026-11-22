"""Master-side native max-weight cap: master and consume refuse the same bundles.

``bundle_weights.validate_weight_rows`` is the single check both paths run. These
tests pin the exact chain arithmetic (subtensor ``check_vec_max_limited``), and run
the real ``master_publish`` and ``slave_consume`` against the chain/SDK fixture to
show a bundle over the cap is never persisted, pinned or pointed to by the master,
and that consume refuses it with the same reason.
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from pathlib import Path

import pytest

import bundle_consume
import bundle_publish
from allocation_encoding import convert_weights_and_uids
from bundle_consume import slave_consume
from bundle_ipfs import compute_raw_cid
from bundle_publish import master_publish
from bundle_store import ArtifactStore
from bundle_weights import native_cap_limit, native_cap_violations, validate_weight_rows
from chain_v2_fake import (
    HOTKEYS,
    MASTER,
    MINER_UIDS,
    NETUID,
    SLAVE,
    FakeChain,
    FakeExtrinsic,
    FakeMetagraph,
    FakeSubtensor,
    FakeWallet,
    build_envelope_for,
    config,
    publish_pointer,
)

E1 = 3600
HALF = 32768 / 65535          # SDK u16_normalized_float(32768)
NINETY = 58982 / 65535


# --------------------------------------------------------------------------- #
# Exact chain arithmetic.
# --------------------------------------------------------------------------- #


def test_limit_recovered_from_sdk_float():
    for limit in (0, 1, 6554, 32768, 58982, 65534, 65535):
        assert native_cap_limit(limit / 65535) == limit


def test_share_exactly_at_cap_is_allowed_and_one_more_is_refused():
    assert native_cap_violations([32768, 32767], 32768) == []      # share == limit/65535
    assert native_cap_violations([32768, 32766], 32768) == [0]


def test_u16_max_limit_disables_the_check():
    assert native_cap_violations([65535], 65535) == []
    assert validate_weight_rows([3], [65535], native_cap=1.0) is None


def test_single_row_is_refused_under_any_cap_below_one():
    reason = validate_weight_rows([3], [65535], native_cap=NINETY)
    assert reason == "row uid 3 share 1.000000 exceeds native max-weight cap 0.900008"


def test_matches_chain_fixed_point_where_float_comparison_differs():
    # 65535*65535 - 512*t == 1: the exact share is above 512/65535 by 1/(65535*t),
    # inside one I32F32 ulp, so the chain's truncating division accepts it. A float
    # comparison would refuse a vector the chain accepts.
    limit = 512
    t = (65535 * 65535 - 1) // limit
    rest = t - 65535
    n = -(-rest // 65535)
    rows = [65535] + [rest // n + (1 if i < rest % n else 0) for i in range(n)]
    assert sum(rows) == t and Fraction(65535, t) > Fraction(limit, 65535)
    assert 65535 / t > limit / 65535
    assert native_cap_violations(rows, limit) == []


def test_every_row_is_checked():
    uids = [1, 2, 3]
    _, w = convert_weights_and_uids(uids, [0.6, 0.2, 0.2])
    reason = validate_weight_rows(uids, w, native_cap=HALF)
    assert reason.startswith("row uid 1 share 0.6")


def test_no_row_is_exempt_from_the_miner_cap():
    # The old owner/burn row exemption is gone: a 0.8 row is refused under the miner cap.
    uids = [0] + list(MINER_UIDS)
    _, w = convert_weights_and_uids(uids, [0.8] + [0.05] * 4)
    assert validate_weight_rows(uids, w, native_cap=NINETY) == "decoded share exceeds cap"


OVER_CAP = [0.7, 0.1, 0.1, 0.1]  # miner uid 3 above a 0.5 native cap


# --------------------------------------------------------------------------- #
# Master publish and slave consume agree (real orchestration, fixture chain).
# --------------------------------------------------------------------------- #


class World:
    def __init__(self, tmp_path, monkeypatch, *, native_cap):
        self.chain = FakeChain(native_cap=native_cap)
        self.sub = FakeSubtensor(self.chain)
        self.mstate = tmp_path / "master"
        self.sstate = tmp_path / "slave"
        self.mcfg = config(self.mstate, ipfs_api_url="http://127.0.0.1:5001", role="master")
        self.scfg = config(self.sstate, ipfs_api_url="http://127.0.0.1:5001")
        self.ipfs_calls = []
        self.blocks = {}
        monkeypatch.setattr(bundle_publish, "publish_block", self._publish)
        monkeypatch.setattr(bundle_publish, "pin_block", lambda *a, **k: self.ipfs_calls.append("pin"))
        monkeypatch.setattr(bundle_publish, "read_back_block", lambda url, cid, data, **k: data)
        monkeypatch.setattr(bundle_consume, "fetch_block", lambda api, cid, **k: self.blocks[cid])
        monkeypatch.setattr(bundle_consume, "fetch_gateway", lambda api, cid, **k: self.blocks[cid])
        owner = (0, HOTKEYS[0])
        monkeypatch.setattr("owner_uid.resolve_owner_uid", lambda *a, **k: owner)

    def _publish(self, url, data, **k):
        self.ipfs_calls.append("add")
        return compute_raw_cid(data)

    def master(self):
        return master_publish(self.sub, FakeMetagraph(), FakeWallet(MASTER), self.mcfg)

    def slave(self):
        return slave_consume(self.sub, FakeMetagraph(), FakeWallet(SLAVE), self.scfg)

    def journal_rows(self, state):
        path = Path(state) / "journal.sqlite3"
        if not path.exists():
            return []
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT operation_kind, state FROM operations").fetchall()
        finally:
            conn.close()


def _stored(state, e):
    return ArtifactStore(str(state)).load(NETUID, e // 360, e)


def _over_cap_weights():
    return convert_weights_and_uids(list(MINER_UIDS), OVER_CAP)[1]


def test_master_refuses_a_bundle_that_weights_the_owner(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, native_cap=NINETY)
    w.chain.advance_to(E1 + 1)
    env = build_envelope_for(w.chain, w.sub, E1, with_owner_row=True)
    monkeypatch.setattr(bundle_publish, "build_envelope", lambda *a, **k: env)

    out = w.master()

    assert out.status == "blocked", out
    assert out.reason == "bundle refused: payload weights the subnet owner (uid 0)", out.reason
    assert _stored(w.mstate, E1) is None and w.ipfs_calls == [] and w.sub.set_commitment_calls == 0


def test_master_refuses_freshly_built_bundle_over_cap(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, native_cap=HALF)
    w.chain.advance_to(E1 + 1)
    env = build_envelope_for(w.chain, w.sub, E1, weights=_over_cap_weights())
    monkeypatch.setattr(bundle_publish, "build_envelope", lambda *a, **k: env)

    out = w.master()

    assert out.status == "blocked", out
    assert out.reason.startswith("bundle refused: row uid 3 share 0.7"), out.reason
    assert "exceeds native max-weight cap 0.500008" in out.reason
    assert _stored(w.mstate, E1) is None          # never persisted
    assert w.ipfs_calls == []                     # never added or pinned
    assert w.sub.set_commitment_calls == 0        # never pointed to
    assert w.journal_rows(w.mstate) == []


def test_master_refuses_previously_stored_bundle_over_cap(tmp_path, monkeypatch):
    # An envelope persisted by an older build is re-validated before publishing.
    w = World(tmp_path, monkeypatch, native_cap=HALF)
    w.chain.advance_to(E1 + 1)
    env = build_envelope_for(w.chain, w.sub, E1, weights=_over_cap_weights())
    w.mstate.mkdir(parents=True)
    ArtifactStore(str(w.mstate)).store(NETUID, E1 // 360, E1, env)

    out = w.master()

    assert out.status == "blocked" and out.reason.startswith("bundle refused: row uid 3"), out
    assert w.ipfs_calls == [] and w.sub.set_commitment_calls == 0
    assert w.journal_rows(w.mstate) == []


@pytest.mark.parametrize("kind", ["over_cap", "owner_row"])
def test_master_and_consume_give_the_same_reason(tmp_path, monkeypatch, kind):
    w = World(tmp_path, monkeypatch, native_cap=HALF)
    w.chain.advance_to(E1 + 1)
    env = (build_envelope_for(w.chain, w.sub, E1, weights=_over_cap_weights()) if kind == "over_cap"
           else build_envelope_for(w.chain, w.sub, E1, with_owner_row=True))
    monkeypatch.setattr(bundle_publish, "build_envelope", lambda *a, **k: env)
    master_out = w.master()

    # Even if such a bundle were pointed to (e.g. by an older master), consume refuses
    # it with the identical reason and nothing reaches the chain.
    w.blocks[publish_pointer(w.chain, env, E1 + 5)] = env
    w.chain.advance_to(E1 + 20)
    slave_out = w.slave()

    assert slave_out.status == "blocked"
    assert master_out.reason == f"bundle refused: {slave_out.reason}"
    assert w.sub.set_weights_calls == 0
    assert w.journal_rows(w.sstate) == []


def test_bundle_within_cap_is_published_and_applied(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, native_cap=NINETY)
    w.chain.advance_to(E1 + 1)
    env = build_envelope_for(w.chain, w.sub, E1)
    monkeypatch.setattr(bundle_publish, "build_envelope", lambda *a, **k: env)

    out = w.master()
    assert out.status == "pending", out
    assert _stored(w.mstate, E1) == env
    assert w.ipfs_calls == ["add", "pin"]

    w.blocks[compute_raw_cid(env)] = env
    w.chain.advance_to(E1 + 20)
    assert w.slave().status == "applied"


@pytest.mark.parametrize("cap,rejected", [(HALF, True), (NINETY, False)])
def test_fixture_chain_rule_agrees_with_the_check(cap, rejected):
    # The fixture chain implements check_vec_max_limited independently; the shared
    # native-cap check refuses exactly what it rejects with MaxWeightExceeded.
    chain = FakeChain(native_cap=cap)
    uids = list(MINER_UIDS)
    _, values = convert_weights_and_uids(uids, [0.8, 0.1, 0.05, 0.05])
    ext = FakeExtrinsic(kind="weights", signer=HOTKEYS[2], nonce=0, birth=0, period=128,
                        payload=(tuple(uids), tuple(values)))
    err = chain._execute(ext, 100)
    assert (err is not None and err["name"] == "MaxWeightExceeded") is rejected
    assert bool(native_cap_violations(values, native_cap_limit(cap))) is rejected
