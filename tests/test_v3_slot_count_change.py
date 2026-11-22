"""Changing INCENTIVE_V3_SLOTS_PER_TIER on a running master.

A table carried from a larger k used to keep every eligible holder while readiness
divided the pool by the new k, so eight full vdb holders with k 8 -> 4 paid 24 readiness
units from a 12-unit pool and broke the 100-unit invariant. update_slots now trims the
table deterministically to the k strongest holders ("shrink" events) before paying.
"""

from __future__ import annotations

import math
from functools import partial

import pytest

from allocation_v3 import MinerEvidence, MinerTierEvidence, allocate_v3
from v3_acceptance import CheckStats
from v3_params import CHECKS_BY_TIER, READINESS_POOL, TOTAL_UNITS
from v3_slots import TierCandidate, update_slots

TOL = 1e-6
FULL = {"stage": "full", "prov_epochs": 5}


def _cand(i, rate, r=0.9):
    return TierCandidate(i, True, r, int(rate * 100), 100)


def test_update_slots_trims_to_the_strongest_k_deterministically():
    rates = {f"m{i}": 0.80 + 0.02 * i for i in range(8)}          # m7 strongest
    cands = {h: _cand(h, rate) for h, rate in rates.items()}
    stages = {h: FULL for h in cands}
    table = {"holders": [f"m{i}" for i in range(8)], "streak": {}}
    t1, ev1 = update_slots(table, cands, stages, 200, k=4)
    t2, ev2 = update_slots(table, cands, stages, 200, k=4)
    assert t1 == t2 and ev1 == ev2                                   # replay-stable
    assert sorted(t1["holders"]) == ["m4", "m5", "m6", "m7"]
    assert sorted(e["identity"] for e in ev1 if e["kind"] == "shrink") == ["m0", "m1", "m2", "m3"]
    # growing k never evicts anyone
    t3, ev3 = update_slots(t1, cands, stages, 201, k=8)
    assert set(t1["holders"]) <= set(t3["holders"]) and not [e for e in ev3 if e["kind"] == "shrink"]


def test_update_slots_shrink_ties_break_by_r_then_identity():
    cands = {"a": _cand("a", 0.9, r=0.5), "b": _cand("b", 0.9, r=0.7), "c": _cand("c", 0.9, r=0.7)}
    stages = {h: FULL for h in cands}
    t, _ = update_slots({"holders": ["a", "b", "c"], "streak": {}}, cands, stages, 1, k=2)
    assert t["holders"] == ["b", "c"]


def _miner(uid):
    return MinerEvidence(uid=uid, hotkey=f"5m{uid}", coldkey=f"5c{uid}", registration_block=1_000 + uid,
                         tiers={"vdb": MinerTierEvidence(
                             checks=tuple(CheckStats(n, 200.0, 1.0) for n in CHECKS_BY_TIER["vdb"]),
                             factor=1.0, h_passes=90 + uid, h_n=100)})


def _eight_full_vdb_holders_state():
    hotkeys = [f"5m{u}" for u in range(1, 9)]
    return {
        "last_epoch": 100,
        "stages": {"vdb": {h: dict(FULL) for h in hotkeys}},
        "carry": {}, "ramps": {}, "identities": {},
        "slots": {"vdb": {"holders": list(hotkeys), "streak": {}}},
    }


def test_scenario_eight_full_holders_k_8_to_4():
    miners = [_miner(u) for u in range(1, 9)]
    state = _eight_full_vdb_holders_state()
    # identities recorded so the allocator does not treat them as new registrations
    state["identities"] = {m.hotkey: [m.uid, m.registration_block] for m in miners}
    res = allocate_v3(miners, {}, state, 200, k=4, redistribute=False)
    assert len(res.holders["vdb"]) == 4
    assert sorted(res.holders["vdb"]) == ["5m5", "5m6", "5m7", "5m8"]   # highest pass rates
    assert len([e for e in res.events if e["kind"] == "shrink"]) == 4
    readiness = sum(res.units_by_uid.values())
    if readiness is not None:
        assert readiness <= READINESS_POOL["vdb"] + TOL
    assert math.isclose(sum(res.units_by_uid.values()) + res.unassigned_units, TOTAL_UNITS, abs_tol=TOL)
    assert res.state["slots"]["vdb"]["holders"] == res.holders["vdb"]


# ------------------------------------------------------------------ actual runtime path

def test_runtime_slot_reduction_between_epochs(tmp_path, monkeypatch):
    """compute_v3_epoch with persisted state: k=4 in one epoch, k=2 in the next."""
    import score_version
    import v3_runtime
    from chain_v2_fake import MASTER, FakeMetagraph, FakeWallet
    from test_incentive_v3_integration import E1, World
    from v3_state import load_epoch_record, load_state

    w = World(tmp_path, monkeypatch)
    w.chain.advance_to(E1 + 5)
    cfg = w.cfg()
    assert v3_runtime.compute_v3_epoch(w.sub, FakeMetagraph(), FakeWallet(MASTER), cfg, w.schedule(E1),
                                    fetch=w.evidence)
    before = load_state(cfg.state_dir, "weights")
    held = {t: len(v["holders"]) for t, v in before["slots"].items()}
    assert max(held.values()) > 2, f"fixture must fill more than two slots in some tier: {held}"

    e2 = E1 + w.chain.tempo
    w.chain.advance_to(e2 + 5)
    cfg2 = cfg
    monkeypatch.setattr(v3_runtime, "allocate_v3", partial(allocate_v3, k=2))
    assert v3_runtime.compute_v3_epoch(w.sub, FakeMetagraph(), FakeWallet(MASTER), cfg2, w.schedule(e2),
                                    fetch=w.evidence)
    rec = load_epoch_record(cfg.state_dir, "weights", e2)
    assert rec is not None, "the reduced-k epoch must produce a record, not fail"
    assert all(len(h) <= 2 for h in rec["holders"].values())
    assert any(e["kind"] == "shrink" for e in rec["events"])
    assert abs(sum(rec["units_by_uid"].values()) - 100.0) < 1e-9  # never burns: all 100 units to miners
