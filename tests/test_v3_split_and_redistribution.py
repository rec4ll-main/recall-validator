"""Configurable readiness split, redistribution and the equal split of allocate_v3 (never burns)."""

from __future__ import annotations

import math

import pytest

from allocation_v3 import MinerEvidence, MinerTierEvidence, allocate_v3
from v3_acceptance import CheckStats
from v3_params import CHECKS_BY_TIER, READINESS_POOL, SERVICE_POOLS, TIER_BUDGET, TOTAL_UNITS, readiness_pools, service_pools

TOL = 1e-6
FULL = {"stage": "full", "prov_epochs": 5}


def _miner(uid, rate, factor=1.0):
    return MinerEvidence(uid=uid, hotkey=f"5m{uid}", coldkey=f"5c{uid}", registration_block=1_000 + uid,
                         tiers={"vdb": MinerTierEvidence(
                             checks=tuple(CheckStats(n, 200.0, rate) for n in CHECKS_BY_TIER["vdb"]),
                             factor=factor, h_passes=90, h_n=100)})


def _state(miners):
    hk = [m.hotkey for m in miners]
    return {"last_epoch": 100, "stages": {"vdb": {h: dict(FULL) for h in hk}}, "carry": {}, "ramps": {},
            "identities": {m.hotkey: [m.uid, m.registration_block] for m in miners},
            "slots": {"vdb": {"holders": list(hk), "streak": {}}}}


def test_default_split_pools_are_unchanged():
    assert readiness_pools(0.40) == READINESS_POOL
    assert service_pools(0.40) == SERVICE_POOLS


@pytest.mark.parametrize("split", [0.0, 0.25, 0.5, 1.0])
def test_split_divides_every_tier_budget(split):
    r, s = readiness_pools(split), service_pools(split)
    for tier, budget in TIER_BUDGET.items():
        service = sum(p for t, p in s.values() if t == tier)
        assert math.isclose(r[tier] + service, budget, abs_tol=TOL)
    assert math.isclose(s["vdb_storage"][1], 2 * s["vdb_query"][1], abs_tol=TOL)
    assert math.isclose(sum(r.values()) + sum(p for _, p in s.values()), TOTAL_UNITS, abs_tol=TOL)


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_split_out_of_range_is_refused(bad):
    with pytest.raises(ValueError):
        readiness_pools(bad)


def test_earned_only_analysis_view_reports_the_unassigned_rest():
    miners = [_miner(1, 0.99), _miner(2, 0.99, factor=0.6)]
    res = allocate_v3(miners, {}, _state(miners), 200, redistribute=False)
    assert res.unassigned_units > 0 and not res.redistributed_by_reason
    assert math.isclose(sum(res.units_by_uid.values()) + res.unassigned_units, TOTAL_UNITS, abs_tol=TOL)


def test_redistribution_pays_everything_to_miners_keeping_ratios():
    miners = [_miner(1, 0.99), _miner(2, 0.99, factor=0.6)]
    earned = allocate_v3(miners, {}, _state(miners), 200, redistribute=False)
    paid = allocate_v3(miners, {}, _state(miners), 200)
    assert paid.unassigned_units == 0.0 and paid.unearned_by_reason == earned.unearned_by_reason
    assert math.isclose(sum(paid.units_by_uid.values()), TOTAL_UNITS, abs_tol=TOL)
    assert math.isclose(sum(paid.redistributed_by_reason.values()), earned.unassigned_units, abs_tol=TOL)
    r_earned = earned.units_by_uid[1] / earned.units_by_uid[2]
    r_paid = paid.units_by_uid[1] / paid.units_by_uid[2]
    assert math.isclose(r_earned, r_paid, rel_tol=1e-9)
    assert paid.state == earned.state  # stages, slots and ramps are mode-independent


def test_no_earner_splits_equally_across_all_miners():
    miners = [_miner(1, 0.0)]
    everyone = {1: "5m1", 2: "5m2", 3: "5m3", 4: "5m4"}
    res = allocate_v3(miners, {}, None, 200, fallback_miners=everyone)
    assert res.unassigned_units == 0.0 and not res.redistributed_by_reason
    assert set(res.units_by_uid) == set(everyone)
    assert all(math.isclose(v, TOTAL_UNITS / 4, abs_tol=TOL) for v in res.units_by_uid.values())
    assert {u: res.hotkey_by_uid[u] for u in everyone} == everyone
    assert math.isclose(res.equal_split_units, TOTAL_UNITS, abs_tol=TOL)


def test_no_earner_without_a_miner_list_refuses_instead_of_burning():
    with pytest.raises(ArithmeticError, match="refusing to burn"):
        allocate_v3([_miner(1, 0.0)], {}, None, 200)


def test_split_changes_readiness_pay():
    miners = [_miner(1, 0.99), _miner(2, 0.99)]
    base = allocate_v3(miners, {}, _state(miners), 200, redistribute=False)
    more = allocate_v3(miners, {}, _state(miners), 200, readiness_split=0.80, redistribute=False)
    assert math.isclose(sum(more.units_by_uid.values()), 2 * sum(base.units_by_uid.values()), rel_tol=1e-9)
