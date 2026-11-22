"""Soak-readiness tests.

1b  metagraph arrays (numpy vs list, alignment, n/uids/axons, deregistered/replaced UIDs)
    and crash-safe state (atomic writes, roll-forward, idempotent resume, no double submit).
1e  missing/NaN/absent evidence never becomes positive credit (property tests).
2   cross-repo: the raw body of the REAL recall-web handler parses + allocates here.
"""

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import score_version
import v3_runtime
import v3_state
from allocation_snapshot import AllocationSnapshotError, build_snapshot
from allocation_v3 import MinerEvidence, MinerTierEvidence, allocate_v3
from chain_v2_fake import (
    GENESIS, HOTKEYS, MASTER, MINERS, NETUID, SLAVE, FakeMetagraph, FakeWallet, publish_pointer,
)
from metagraph_arrays import MetagraphAlignmentError, aligned_identity, as_list
from setter_guard import SetterGuardError, other_active_setters
from v3_acceptance import CheckStats
from v3_demand import ramp_step
from v3_metrics_client import V3MetricsError, parse_v3_metrics, to_miner_evidence
from v3_params import CHECKS_BY_TIER, DEMAND_DELTA_UP, DEMAND_W, SERVICE_POOLS, TIERS
from v3_state import load_epoch_record, load_state

from test_incentive_v3_integration import E1, World, _body

HANDLER_FIXTURE = Path(__file__).parent / "fixtures" / "recall_v3_metrics_from_web_handler.json"


@pytest.fixture(autouse=True)
def _reset_version():
    yield


# ------------------------------------------------------------------ 1b metagraph arrays

class NumpyMetagraph(FakeMetagraph):
    """Shapes as the SDK returns them: lists for keys, numpy for numeric arrays."""

    def __init__(self, mutate=None):
        super().__init__()
        self._mutate = mutate

    def sync(self, subtensor=None, block=None, lite=True):
        super().sync(subtensor, block, lite)
        n = len(self.hotkeys)
        self.block_at_registration = np.array(self.block_at_registration, dtype=np.int64)
        self.uids = np.arange(n, dtype=np.int64)
        self.n = np.array(n)
        self.validator_permit = np.array([True] * n)
        self.last_update = np.array([0] * n, dtype=np.int64)
        self.axons = [SimpleNamespace(ip="0.0.0.0", port=0) for _ in range(n)]
        if self._mutate:
            self._mutate(self)


def test_as_list_handles_numpy_tuple_list_none():
    assert as_list(np.array([1, 2, 3])) == [1, 2, 3]
    assert as_list((1, 2)) == [1, 2] and as_list([True]) == [True] and as_list(None) == []
    with pytest.raises(MetagraphAlignmentError):
        as_list("abc")
    with pytest.raises(MetagraphAlignmentError):
        as_list(np.array(5))  # 0-d


def test_numpy_metagraph_snapshot_matches_list_metagraph():
    w = World.__new__(World)
    from chain_v2_fake import FakeChain, FakeSubtensor
    chain = FakeChain(); sub = FakeSubtensor(chain); chain.advance_to(E1 + 5)
    a = build_snapshot(sub, FakeMetagraph(), NETUID, None, GENESIS, block=E1)
    b = build_snapshot(sub, NumpyMetagraph(), NETUID, None, GENESIS, block=E1)
    assert a.cohort == b.cohort and a.registration_blocks == b.registration_blocks
    assert all(type(r) is int for r in b.registration_blocks)


@pytest.mark.parametrize("mutate,msg", [
    (lambda m: setattr(m, "coldkeys", m.coldkeys[:-1]), "length mismatch"),
    (lambda m: setattr(m, "block_at_registration", m.block_at_registration[:-1]), "length mismatch"),
    (lambda m: setattr(m, "uids", m.uids[::-1].copy()), "0..n-1"),
    (lambda m: setattr(m, "uids", m.uids[:-1]), "0..n-1"),
    (lambda m: setattr(m, "n", np.array(len(m.hotkeys) + 1)), "n disagrees"),
    (lambda m: setattr(m, "axons", m.axons[:-1]), "axons length"),
    (lambda m: setattr(m, "validator_permit", m.validator_permit[:-1]), "validator_permit length"),
    (lambda m: setattr(m, "hotkeys", [m.hotkeys[0]] + m.hotkeys[:-1]), "duplicate hotkey"),
    (lambda m: setattr(m, "block_at_registration", np.array([-1] * len(m.hotkeys))), "registration"),
])
def test_misaligned_metagraph_fails_closed(mutate, msg):
    from chain_v2_fake import FakeChain, FakeSubtensor
    chain = FakeChain(); sub = FakeSubtensor(chain); chain.advance_to(E1 + 5)
    with pytest.raises(AllocationSnapshotError, match=msg):
        build_snapshot(sub, NumpyMetagraph(mutate), NETUID, None, GENESIS, block=E1)


def test_setter_guard_accepts_numpy_arrays():
    m = SimpleNamespace(hotkeys=["a", "b", "c"], validator_permit=np.array([True, True, False]),
                        last_update=np.array([100, 990, 995]))
    # The previous `array or []` form raised "truth value ... ambiguous" here.
    assert other_active_setters(m, "a", 1000, 50) == ["b"]
    bad = SimpleNamespace(hotkeys=["a", "b"], validator_permit=np.array([True]), last_update=np.array([1, 2]))
    with pytest.raises(SetterGuardError):
        other_active_setters(bad, "a", 1000, 50)


def _tier_ok(tier, p=0.99, neff=200.0, credited=None):
    return MinerTierEvidence(tuple(CheckStats(c, neff, p) for c in CHECKS_BY_TIER[tier]), 1.0,
                             int(432 * p), 432, credited)


def _m(uid, hk, reg=100, tiers=None, serious=False, ck=None):
    return MinerEvidence(uid, hk, ck or ("ck-" + hk), reg, tiers if tiers is not None else {"vdb": _tier_ok("vdb")}, serious)


def _run_epochs(seq, state=None, start=E1, step=360):
    res = None
    for i, miners in enumerate(seq):
        res = allocate_v3(miners, {k: 0.0 for k in SERVICE_POOLS}, state, start + i * step, redistribute=False)
        state = res.state
    return res, state


def test_reregistered_hotkey_restarts_from_observation():
    res, st = _run_epochs([[_m(3, "hk")]] * 8)
    assert st["stages"]["vdb"]["hk"]["stage"] == "full" and "hk" in st["slots"]["vdb"]["holders"]
    # same hotkey, deregistered and re-registered (new registration block, new uid)
    res, st2 = _run_epochs([[_m(5, "hk", reg=9000)]], state=st, start=E1 + 8 * 360)
    assert any(e["kind"] == "identity_reset" and e["identity"] == "hk" for e in res.events)
    full_units = _run_epochs([[_m(3, "hk")]] * 9)[0].units_by_uid[3]
    # restarted: one eligible epoch after reset = provisional/1 (g = 0.5), not full
    assert st2["stages"]["vdb"]["hk"] == {"stage": "provisional", "prov_epochs": 1}
    assert res.units_by_uid.get(5, 0.0) == pytest.approx(full_units * 0.5)


def test_replaced_uid_new_hotkey_inherits_nothing():
    res, st = _run_epochs([[_m(3, "old")]] * 8)
    res, st2 = _run_epochs([[_m(3, "new", reg=9000)]], state=st, start=E1 + 8 * 360)
    assert "old" not in st2["stages"]["vdb"] and "old" not in st2["slots"]["vdb"]["holders"]
    assert st2["stages"]["vdb"]["new"] == {"stage": "provisional", "prov_epochs": 1}
    full_units = _run_epochs([[_m(3, "old")]] * 9)[0].units_by_uid[3]
    assert res.units_by_uid.get(3, 0.0) == pytest.approx(full_units * 0.5)


def test_duplicate_uid_in_cohort_refused():
    with pytest.raises(ValueError, match="duplicate uid"):
        allocate_v3([_m(3, "a", ck="x"), _m(3, "b", ck="y")], {k: 0.0 for k in SERVICE_POOLS}, None, E1, redistribute=False)


def test_evidence_for_moved_uid_is_rejected():
    body = _body(E1)
    cohort = {MINERS[i].ss58_address: 3 + i for i in range(4)}
    ev = parse_v3_metrics(body, NETUID, E1, cohort)
    snap = [(3 + i + (1 if i == 0 else 0), MINERS[i].ss58_address, "ck", 10) for i in range(4)]
    with pytest.raises(V3MetricsError):
        to_miner_evidence(ev, snap)


# ------------------------------------------------------------------ 1b crash safety

def test_atomic_create_never_leaves_torn_record(tmp_path, monkeypatch):
    def crash(*a, **k):
        raise OSError("simulated crash during publish")
    monkeypatch.setattr(v3_state.os, "link", crash)
    with pytest.raises(OSError):
        v3_state.write_epoch_record(str(tmp_path), "weights", E1, {"x": 1})
    d = tmp_path / "v3" / "weights" / "epochs"
    assert not (d / f"{E1}.json").exists() and list(d.glob("*.tmp")) == []


def test_record_is_exclusive_and_complete(tmp_path):
    v3_state.write_epoch_record(str(tmp_path), "weights", E1, {"a": 1})
    with pytest.raises(FileExistsError):
        v3_state.write_epoch_record(str(tmp_path), "weights", E1, {"a": 2})
    assert load_epoch_record(str(tmp_path), "weights", E1) == {"a": 1}


def test_stale_tmp_files_are_ignored_and_cleaned(tmp_path):
    d = tmp_path / "v3" / "weights" / "epochs"; d.mkdir(parents=True)
    (d / f".{E1}.json.abc.tmp").write_text("{torn")
    assert v3_state.latest_epoch_record(str(tmp_path), "weights") is None
    assert v3_state.cleanup_tmp(str(tmp_path), "weights") == 1


def _runtime_epochs(w, cfg, epochs, crash_at=None, monkeypatch=None):
    """Run v3 epochs; crash_at = (epoch, point) with point in {'before_record','after_record'}."""
    for e in epochs:
        w.chain.advance_to(e + 5)
        if crash_at and crash_at[0] == e:
            point = crash_at[1]
            orig_save, orig_write = v3_state.save_state, v3_state.write_epoch_record
            if point == "after_record":
                monkeypatch.setattr(v3_runtime, "save_state", lambda *a, **k: (_ for _ in ()).throw(OSError("kill")))
            else:
                monkeypatch.setattr(v3_runtime, "write_epoch_record", lambda *a, **k: (_ for _ in ()).throw(OSError("kill")))
            with pytest.raises(OSError):
                v3_runtime.compute_v3_epoch(w.sub, FakeMetagraph(), FakeWallet(MASTER), cfg,
                                            w.schedule(e), fetch=w.evidence)
            monkeypatch.setattr(v3_runtime, "save_state", orig_save)
            monkeypatch.setattr(v3_runtime, "write_epoch_record", orig_write)
        v3_runtime.compute_v3_epoch(w.sub, FakeMetagraph(), FakeWallet(MASTER), cfg,
                                    w.schedule(e), fetch=w.evidence)


@pytest.mark.parametrize("point", ["before_record", "after_record"])
@pytest.mark.parametrize("crash_epoch_idx", [0, 2, 5])
def test_restart_mid_run_matches_uninterrupted_run(tmp_path, monkeypatch, point, crash_epoch_idx):
    epochs = [E1 + i * 360 for i in range(7)]
    ref = World(tmp_path / "ref", monkeypatch); cref = ref.cfg()
    _runtime_epochs(ref, cref, epochs)
    w = World(tmp_path / "crash", monkeypatch); c = w.cfg()
    _runtime_epochs(w, c, epochs, crash_at=(epochs[crash_epoch_idx], point), monkeypatch=monkeypatch)
    for e in epochs:
        a, b = load_epoch_record(cref.state_dir, "weights", e), load_epoch_record(c.state_dir, "weights", e)
        for k in ("units_by_uid", "unearned_by_reason", "weights_u16", "uids", "state_after", "inputs_digest"):
            assert a[k] == b[k], (e, k)
    assert load_state(cref.state_dir, "weights") == load_state(c.state_dir, "weights")


def test_state_ahead_without_record_fails_closed(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch); c = w.cfg(); w.chain.advance_to(E1 + 5)
    v3_state.save_state(c.state_dir, "weights", {"last_epoch": E1, "stages": {}, "carry": {}, "slots": {}, "ramps": {}})
    with pytest.raises(RuntimeError, match="ahead"):
        v3_runtime.compute_v3_epoch(w.sub, FakeMetagraph(), FakeWallet(MASTER), c, w.schedule(E1),
                                    fetch=w.evidence)
    assert w.fetch_calls == 1 and load_epoch_record(c.state_dir, "weights", E1) is None


def test_weights_mode_restart_rebuilds_identical_vector_and_no_double_submit(tmp_path, monkeypatch):
    from bundle_consume import slave_consume
    from bundle_envelope import build_envelope
    from bundle_v2 import parse_bundle
    w = World(tmp_path, monkeypatch); w.chain.advance_to(E1 + 5)
    monkeypatch.setattr(v3_runtime, "fetch_v3_metrics", w.evidence)
    env1 = build_envelope(w.sub, FakeMetagraph(), FakeWallet(MASTER), w.cfg(), w.schedule(E1), fetch=None)
    st1 = load_state(w.cfg().state_dir, "weights")
    # master restarts mid-epoch and rebuilds: same record -> same vector, no state advance, no refetch
    env2 = build_envelope(w.sub, FakeMetagraph(), FakeWallet(MASTER), w.cfg(), w.schedule(E1), fetch=None)
    p1, p2 = parse_bundle(env1).payload, parse_bundle(env2).payload
    assert (p1.uids, p1.weights_u16) == (p2.uids, p2.weights_u16) and w.fetch_calls == 1
    assert load_state(w.cfg().state_dir, "weights") == st1
    w.blocks[publish_pointer(w.chain, env1, E1 + 5)] = env1
    w.chain.advance_to(E1 + 20)
    slave_cfg = w.cfg(role="slave")
    out1 = slave_consume(w.sub, FakeMetagraph(), FakeWallet(SLAVE), slave_cfg)
    n1 = len(w.chain.applied_weight_sets)
    out2 = slave_consume(w.sub, FakeMetagraph(), FakeWallet(SLAVE), slave_cfg)  # restart / second tick
    assert out1.status == "applied" and out2.reason == "already observed"
    assert len(w.chain.applied_weight_sets) == n1 == 1      # journal: exactly one set_weights


# ------------------------------------------------------------------ 1e no positive default credit

def test_missing_service_credit_leaves_whole_pool_unearned():
    miners = [_m(u, f"h{u}", tiers={t: _tier_ok(t) for t in TIERS}) for u in range(3, 7)]
    st = None
    for i in range(10):
        r = allocate_v3(miners, dict(DEMAND_W), st, E1 + i * 360, redistribute=False); st = r.state
    for name in SERVICE_POOLS:
        if name != "vdb_storage":
            assert r.unearned_by_reason[f"service_unpaid:{name}"] == pytest.approx(SERVICE_POOLS[name][1])


def test_nan_factor_and_garbage_never_pay():
    t = MinerTierEvidence(tuple(CheckStats(c, 200.0, 0.99) for c in CHECKS_BY_TIER["vdb"]), float("nan"), 400, 432, None)
    st = None
    for i in range(8):
        r = allocate_v3([_m(3, "a", tiers={"vdb": t})], {k: 0.0 for k in SERVICE_POOLS}, st, E1 + i * 360, redistribute=False); st = r.state
    assert r.units_by_uid.get(3, 0.0) == 0.0


@pytest.mark.parametrize("bad", [{"L": float("nan"), "f": 0.9}, {"L": -5, "f": 7}, {"L": "x", "f": None},
                                 {"L": float("inf"), "f": float("nan")}])
def test_corrupted_ramp_state_cannot_jump(bad):
    _, f = ramp_step(bad, 1000.0, 1000.0)
    assert 0.0 <= f <= DEMAND_DELTA_UP + 1e-12


def test_allocation_property_fuzz():
    rng = random.Random(20261008)
    weird = [0.0, 1.0, 0.5, 0.999, 1e-12]
    for it in range(400):
        n = rng.randint(0, 8)
        miners = []
        for u in range(n):
            tiers = {}
            for t in TIERS:
                if rng.random() < 0.5:
                    continue
                checks = tuple(CheckStats(c, rng.choice([0.0, 10.0, 80.0, 500.0]), rng.choice(weird))
                               for c in CHECKS_BY_TIER[t] if rng.random() < 0.9)
                cred = None
                if rng.random() < 0.5:
                    cred = {k: rng.choice([0.0, 1.0, 3.5]) for k, (pt, _) in SERVICE_POOLS.items() if pt == t}
                tiers[t] = MinerTierEvidence(checks, rng.choice([0.0, 1.0, 0.7, float("nan"), float("inf"), -1.0]),
                                             rng.randint(0, 50), 50, cred)
            miners.append(_m(u + 3, f"h{u}", tiers=tiers, serious=rng.random() < 0.1, ck=f"c{rng.randint(0, 5)}"))
        demand = {k: rng.choice([0.0, DEMAND_W[k], 10 * DEMAND_W[k]]) for k in SERVICE_POOLS}
        st = None
        for e in range(rng.randint(1, 6)):
            r = allocate_v3(miners, demand, st, E1 + e * 360, redistribute=False); st = r.state
            vals = list(r.units_by_uid.values())
            assert all(math.isfinite(v) and v > 0 for v in vals)
            assert sum(vals) + r.unassigned_units == pytest.approx(100.0, abs=1e-9)
            ev_uids = {m.uid for m in miners if m.tiers and not m.serious_failure}
            assert set(r.units_by_uid) <= ev_uids  # no evidence / serious -> nothing


@pytest.mark.parametrize("mut", [
    lambda c: c.update(n_eff=float("nan")), lambda c: c.update(p_hat=float("inf")),
    lambda c: c.update(n_eff=-1), lambda c: c.update(p_hat=1.5), lambda c: c.update(n_eff=True),
    lambda c: c.update(n_eff="12"), lambda c: c.update(blocked="x", n_eff=5.0),
    lambda c: c.update(blocked=7), lambda c: c.pop("n_eff"),
])
def test_parser_rejects_non_numeric_evidence(mut):
    body = json.loads(HANDLER_FIXTURE.read_text())
    mut(body["miners"][0]["tiers"]["emb"]["checks"][0])
    with pytest.raises(V3MetricsError):
        parse_v3_metrics(body, 9999, 8174160, {"hk-a": 11, "hk-b": 12, "hk-c": 13, "hk-d": 14})


# ------------------------------------------------------------------ 2 cross-repo (real handler output)

def test_real_web_handler_output_parses_and_allocates():
    body = json.loads(HANDLER_FIXTURE.read_text())
    cohort = {"hk-a": 11, "hk-b": 12, "hk-c": 13, "hk-d": 14}
    ev = parse_v3_metrics(body, 9999, 8174160, cohort)
    snap = [(11, "hk-a", "ck-a", 10), (12, "hk-b", "ck-b", 11), (13, "hk-c", "ck-c", 12), (14, "hk-d", "ck-d", 13)]
    miners = to_miner_evidence(ev, snap)
    st, r = None, None
    for i in range(8):  # observation -> provisional -> full under constant evidence
        r = allocate_v3(miners, ev.demand, st, 8174160 + i * 360, redistribute=False); st = r.state
    assert set(r.units_by_uid) == {11}                 # only hk-a qualifies
    assert set(r.eligible["vdb"]) == {"hk-a"}          # hk-b integrity ~0.75 < 85/89
    assert set(r.eligible["emb"]) == {"hk-a"} and set(r.eligible["rerank"]) == {"hk-a"}
    assert r.eligible["gen"] == []                     # generation BLOCKED (no corpus/backend)
    assert r.unearned_by_reason["service_unpaid:emb"] == pytest.approx(SERVICE_POOLS["emb"][1])  # no demand sourced
    assert sum(r.units_by_uid.values()) + r.unassigned_units == pytest.approx(100.0, abs=1e-9)
    assert r.f["vdb_query"] > 0 and r.unearned_by_reason["storage_disabled"] == pytest.approx(SERVICE_POOLS["vdb_storage"][1])
