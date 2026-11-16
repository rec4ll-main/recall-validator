"""Incentive v3 (score_version 11) unit tests: acceptance, slots, demand, allocator.

Fixtures are ported from the offline synthetic harness.
"""

from __future__ import annotations

import math
import random

import pytest

from allocation_v3 import MinerEvidence, MinerTierEvidence, allocate_v3, encode_v3
from v3_acceptance import CheckStats, decide_check, decide_tier, design
from v3_demand import ramp_step
from v3_params import (
    CHECKS,
    CHECKS_BY_TIER,
    DEMAND_DELTA_UP,
    DEMAND_LAMBDA,
    READINESS_POOL,
    SERVICE_POOLS,
    STORAGE_REWARDS_ENABLED,
    TIERS,
)
from v3_slots import SlotRule, TierCandidate, advance_stage, update_slots, z_stat

# ----------------------------------------------------------------- acceptance

@pytest.mark.parametrize("name", sorted(CHECKS))
def test_design_table_matches_exact_binomial(name):
    _, pg, pb, fn, fp, n, c = CHECKS[name]
    assert design(pg, pb, fn, fp) == (n, c)


def test_v2_wilson_cliff_is_gone_good_miner_passes():
    # spec v3 sec 3.1: v2 needed 10/10; v3 VDB check: p_hat 0.97 at n_eff 120 passes.
    d = decide_check(CheckStats("vdb_integrity", 120.0, 0.97), None, 0)
    assert d.eligible and d.status == "pass"


def test_bad_miner_fails_with_enough_evidence():
    d = decide_check(CheckStats("vdb_integrity", 200.0, 0.90), True, 0)
    assert not d.eligible and d.status == "fail"


def test_insufficient_evidence_carries_at_most_two_epochs():
    s = CheckStats("emb_quality", 10.0, 1.0)
    d1 = decide_check(s, True, 0)
    d2 = decide_check(s, d1.eligible, d1.short_streak)
    d3 = decide_check(s, d2.eligible, d2.short_streak)
    assert (d1.status, d2.status, d3.status) == ("carried", "carried", "insufficient")
    assert d1.eligible and d2.eligible and not d3.eligible


def test_insufficient_without_history_is_ineligible():
    d = decide_check(CheckStats("emb_quality", 10.0, 1.0), None, 0)
    assert not d.eligible and d.status == "insufficient"


def test_fail_is_never_carried():
    d = decide_check(CheckStats("vdb_integrity", 200.0, 0.5), True, 0)
    assert not d.eligible


def test_tier_requires_every_check_missing_is_insufficient():
    ok, dec = decide_tier("vdb", {"vdb_integrity": CheckStats("vdb_integrity", 200.0, 0.99)}, {})
    assert not ok and dec["vdb_retention"].status == "insufficient"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0])
def test_non_finite_or_negative_evidence_rejected(bad):
    with pytest.raises(ValueError):
        decide_check(CheckStats("vdb_integrity", bad, 0.9), None, 0)


def test_p_hat_out_of_range_rejected():
    with pytest.raises(ValueError):
        decide_check(CheckStats("vdb_integrity", 100.0, 1.5), None, 0)

# ----------------------------------------------------------------- stages / slots

def test_stage_progression_and_reset():
    s = None
    seq = []
    for ok in [True] * 5 + [False] + [True] * 6:
        s = advance_stage(s, ok)
        seq.append(s["stage"])
    assert seq[0] == "provisional" and seq[4] == "full"
    assert seq[5] == "full"  # full is not reset by one ineligible epoch (slot drop handles it)
    s = advance_stage({"stage": "provisional", "prov_epochs": 3}, False)
    assert s["stage"] == "observation"


def _cand(i, r, hp, hn, ok=True):
    return TierCandidate(i, ok, r, hp, hn)


FULL = {"stage": "full", "prov_epochs": 5}


def test_fill_then_drop_ineligible_holder():
    cands = {f"m{i}": _cand(f"m{i}", 0.9, 90, 100) for i in range(5)}
    stages = {k: FULL for k in cands}
    t, ev = update_slots({}, cands, stages, 1)
    assert len(t["holders"]) == 4 and sum(e["kind"] == "fill" for e in ev) == 4
    h0 = t["holders"][0]
    cands[h0] = _cand(h0, 0.0, 0, 100, ok=False)
    t2, ev2 = update_slots(t, cands, stages, 2)
    assert h0 not in t2["holders"] and len(t2["holders"]) == 4
    assert [e["kind"] for e in ev2] == ["drop", "fill"]


def test_z_rule_needs_significance_not_margin():
    cands = {f"m{i}": _cand(f"m{i}", 0.90, 390, 432) for i in range(4)}
    cands["c"] = _cand("c", 0.97, 405, 432)       # +0.035 on 432 probes: z ~ 1.9
    stages = {k: FULL for k in cands}
    t, _ = update_slots({}, {k: v for k, v in cands.items() if k != "c"}, stages, 1)
    t2, ev = update_slots(t, cands, stages, 2, rule=SlotRule("Z", 3.0, 1))
    assert "c" not in t2["holders"] and not any(e["kind"] == "displace" for e in ev)
    cands["c"] = _cand("c", 0.99, 428, 432)       # +0.087: z ~ 5
    t3, ev3 = update_slots(t2, cands, stages, 3, rule=SlotRule("Z", 3.0, 1))
    assert "c" in t3["holders"] and sum(e["kind"] == "displace" for e in ev3) == 1


def test_only_full_stage_challengers_displace():
    cands = {f"m{i}": _cand(f"m{i}", 0.80, 340, 432) for i in range(4)}
    cands["c"] = _cand("c", 0.99, 430, 432)
    stages = {k: FULL for k in cands}
    stages["c"] = {"stage": "provisional", "prov_epochs": 2}
    t, _ = update_slots({}, {k: v for k, v in cands.items() if k != "c"}, stages, 1)
    t2, ev = update_slots(t, cands, stages, 2)
    assert "c" not in t2["holders"]


def test_at_most_one_displacement_per_epoch():
    cands = {f"m{i}": _cand(f"m{i}", 0.80, 340, 432) for i in range(4)}
    for j in range(3):
        cands[f"c{j}"] = _cand(f"c{j}", 0.99, 430, 432)
    stages = {k: FULL for k in cands}
    t, _ = update_slots({}, {k: v for k, v in cands.items() if k.startswith("m")}, stages, 1)
    per_epoch = []
    for e in range(2, 8):
        t, ev = update_slots(t, cands, stages, e)
        per_epoch.append(sum(x["kind"] == "displace" for x in ev))
    # default rule needs 3 consecutive significant epochs, then <= 1 displacement per epoch
    assert per_epoch[:2] == [0, 0] and max(per_epoch) == 1 and sum(per_epoch) == 3


def test_default_rule_is_harness_v2_selection():
    from v3_params import SLOT_RULE_CONFIRM_EPOCHS, SLOT_RULE_KIND, SLOT_RULE_WINDOW_HOURS, SLOT_RULE_Z
    assert (SLOT_RULE_KIND, SLOT_RULE_Z, SLOT_RULE_WINDOW_HOURS, SLOT_RULE_CONFIRM_EPOCHS) == ("Z", 3.0, 48, 3)


def test_identical_miners_no_displacement_harness_regression():
    """H2 regression: identical 0.90 miners, 200 epochs, sampled counts; default rule."""
    rng = random.Random(20261008)
    names = [f"m{i}" for i in range(5)]
    stages = {k: FULL for k in names}
    t = {}
    displaced = 0
    for e in range(200):
        cands = {}
        for k in names:
            n = 432
            p = sum(rng.random() < 0.9 for _ in range(n))
            cands[k] = _cand(k, p / n, p, n)
        t, ev = update_slots(t, cands, stages, e)
        displaced += sum(x["kind"] == "displace" for x in ev)
    assert displaced == 0


def test_z_stat_symmetry():
    assert z_stat((90, 100), (80, 100)) == pytest.approx(-z_stat((80, 100), (90, 100)))
    assert z_stat((0, 0), (5, 10)) == 0.0

# ----------------------------------------------------------------- demand

def test_lambda_value():
    assert abs(DEMAND_LAMBDA - 0.0670) < 5e-4


def _slew_fixture(L_frac, quiet, D_frac, steps):
    W = 1000.0
    st = {"L": L_frac * W, "f": 0.0}
    for _ in range(quiet):
        st, _ = ramp_step(st, 0.0, W)
    out = []
    for _ in range(steps):
        st, f = ramp_step(st, D_frac * W, W)
        out.append(f)
    return out


@pytest.mark.parametrize("L,quiet,D,expected", [
    (0.6, 7, 0.6, [0.25, 0.399]),
    (1.0, 7, 1.0, [0.25, 0.50, 0.688]),
    (1.0, 1, 1.0, [0.25, 0.50, 0.75, 0.949]),
])
def test_slew_fixtures_match_harness(L, quiet, D, expected):
    got = _slew_fixture(L, quiet, D, len(expected))
    assert all(abs(g - x) < 1e-3 for g, x in zip(got, expected)), got


def test_slew_property_2000_steps():
    rng = random.Random(7)
    st, prev = None, 0.0
    for _ in range(2000):
        D = rng.choice([0.0, rng.uniform(0, 3000)])
        st, f = ramp_step(st, D, 1000.0)
        assert f - prev <= DEMAND_DELTA_UP + 1e-12 and 0 <= f <= min(1.0, D / 1000.0) + 1e-12
        prev = f


def test_quiet_epoch_pays_zero_no_downward_limit():
    st, f = ramp_step({"L": 1000.0, "f": 1.0}, 0.0, 1000.0)
    assert f == 0.0


@pytest.mark.parametrize("D", [math.nan, -1.0, math.inf])
def test_bad_demand_rejected(D):
    with pytest.raises(ValueError):
        ramp_step(None, D, 1000.0)

# ----------------------------------------------------------------- allocator

def _healthy_tier(tier, p=0.99, neff=200.0, factor=1.0, credited=None):
    return MinerTierEvidence(tuple(CheckStats(c, neff, p) for c in CHECKS_BY_TIER[tier]),
                             factor, int(432 * p), 432, credited)


def _miner(uid, tiers, ck=None, reg=100, serious=False):
    return MinerEvidence(uid, f"hk{uid}", ck or f"ck{uid}", reg, tiers, serious)


def _run(miners, demand, epochs, state=None, start=1000):
    res = None
    for e in range(epochs):
        res = allocate_v3(miners, demand, state, start + e * 360)
        state = res.state
    return res, state


ZERO = {k: 0.0 for k in SERVICE_POOLS}


def test_conservation_every_epoch_sum_is_100():
    miners = [_miner(u, {t: _healthy_tier(t) for t in TIERS}) for u in range(3, 8)]
    state = None
    rng = random.Random(1)
    for e in range(30):
        demand = {k: rng.uniform(0, 3000) for k in SERVICE_POOLS}
        r = allocate_v3(miners, demand, state, 1000 + 360 * e)
        state = r.state
        assert abs(sum(r.units_by_uid.values()) + r.burn_units - 100.0) < 1e-9
        assert abs(sum(r.burn_by_reason.values()) - r.burn_units) < 1e-6


def test_storage_pool_always_burns():
    assert STORAGE_REWARDS_ENABLED is False
    miners = [_miner(u, {t: _healthy_tier(t) for t in TIERS}) for u in range(3, 7)]
    r, _ = _run(miners, {k: 1e9 for k in SERVICE_POOLS}, 12)
    assert r.burn_by_reason["storage_disabled"] == pytest.approx(SERVICE_POOLS["vdb_storage"][1])


def test_todays_quiet_vdb_only_readiness():
    """Quiet network: only VDB checks can be satisfied; no demand. Full-stage holders
    with r ~0.99 earn ~12 x 0.99 readiness units in total; everything else burns."""
    miners = [_miner(u, {"vdb": _healthy_tier("vdb")}) for u in range(3, 7)]
    r, _ = _run(miners, ZERO, 8)
    paid = sum(r.units_by_uid.values())
    assert paid == pytest.approx(READINESS_POOL["vdb"] * 0.99, abs=1e-9)
    assert r.burn_units == pytest.approx(100.0 - paid)


@pytest.mark.parametrize("epochs", [20, 60])
def test_healthy_capabilities_full_demand_scenario(epochs):
    """HEALTHY-capabilities (modeled): all tiers eligible, credited demand = W every epoch.
    f follows the EWMA level: f_e = L_e/W = 1 - (1 - lambda)^e (slew never binds here)."""
    from v3_params import DEMAND_W
    # every miner carries credited service for each pool of its tier (missing credit = 0)
    cred = {t: {k: 1.0 for k, (pt, _) in SERVICE_POOLS.items() if pt == t} for t in TIERS}
    miners = [_miner(u, {t: _healthy_tier(t, credited=cred[t]) for t in TIERS}) for u in range(3, 7)]
    r, _ = _run(miners, dict(DEMAND_W), epochs)
    f = 1.0 - (1.0 - DEMAND_LAMBDA) ** epochs
    readiness = sum(READINESS_POOL.values()) * 0.99
    service_max = sum(p for k, (_, p) in SERVICE_POOLS.items() if k != "vdb_storage")
    paid = sum(r.units_by_uid.values())
    assert all(v == pytest.approx(f) for k, v in r.f.items())
    assert paid == pytest.approx(readiness + f * service_max, abs=1e-9)
    assert r.burn_by_reason["storage_disabled"] == pytest.approx(12.0)
    assert r.burn_by_reason["storage_disabled"] == pytest.approx(12.0)


def test_new_miner_stages_provisional_half_weight():
    miners = [_miner(3, {"vdb": _healthy_tier("vdb")})]
    r1 = allocate_v3(miners, ZERO, None, 1000)
    assert r1.units_by_uid[3] == pytest.approx(12.0 / 4 * 0.99 * 0.5)
    r5, _ = _run(miners, ZERO, 5)
    assert r5.units_by_uid[3] == pytest.approx(12.0 / 4 * 0.99)


def test_serious_failure_zeroes_and_resets_stage():
    good = {t: _healthy_tier(t) for t in ("vdb",)}
    m = _miner(3, good)
    _, st = _run([m], ZERO, 6)
    r = allocate_v3([_miner(3, good, serious=True)], ZERO, st, 1000 + 360 * 6)
    assert 3 not in r.units_by_uid
    assert r.state["stages"]["vdb"]["hk3"]["stage"] == "observation"


def test_coldkey_dedup_highest_registration():
    a = _miner(3, {"vdb": _healthy_tier("vdb")}, ck="same", reg=10)
    b = _miner(4, {"vdb": _healthy_tier("vdb")}, ck="same", reg=20)
    r = allocate_v3([a, b], ZERO, None, 1000)
    assert set(r.units_by_uid) == {4}


def test_same_epoch_twice_refused():
    m = [_miner(3, {"vdb": _healthy_tier("vdb")})]
    r = allocate_v3(m, ZERO, None, 1000)
    with pytest.raises(ValueError):
        allocate_v3(m, ZERO, r.state, 1000)


def test_per_miner_cap_overflow_burns():
    """25 eligible emb miners -> cap = max(10, 200/25) = 10; all credited emb service on
    one miner (13.2 x f) plus its readiness exceeds 10 -> overflow burns."""
    from v3_params import DEMAND_W
    ms = [_miner(u, {"emb": _healthy_tier("emb", credited={"emb": 1.0 if u == 3 else 0.0})})
          for u in range(3, 28)]
    r, _ = _run(ms, dict(DEMAND_W), 60)
    assert 10.0 - 0.01 < r.units_by_uid[3] < 10.0  # cap less the u16 rounding margin
    assert r.burn_by_reason["per_miner_cap"] > 0


def test_service_paid_only_to_tier_eligible():
    from v3_params import DEMAND_W
    elig = _miner(3, {"emb": _healthy_tier("emb")})
    inel = _miner(4, {"emb": _healthy_tier("emb", p=0.5)})
    r, _ = _run([elig, inel], dict(DEMAND_W), 10)
    assert 4 not in r.units_by_uid and r.units_by_uid[3] > 0


def test_encode_has_burn_row_and_ascending_uids():
    m = [_miner(u, {"vdb": _healthy_tier("vdb")}) for u in (5, 3, 6)]
    r, _ = _run(m, ZERO, 6)
    uids, hks, vals = encode_v3(r, 0, "owner")
    assert uids == sorted(uids) and uids[0] == 0 and hks[0] == "owner"
    shares = [v / sum(vals) for v in vals]
    assert shares[0] == pytest.approx(r.burn_units / 100.0, abs=2e-4)


def test_encode_refuses_burn_uid_collision():
    m = [_miner(3, {"vdb": _healthy_tier("vdb")})]
    r = allocate_v3(m, ZERO, None, 1000)
    with pytest.raises(ValueError):
        encode_v3(r, 3, "x")

def test_blocked_check_never_carries_or_advances():
    """Explicit coverage block: ineligible, streak reset, never preserves eligibility."""
    # Previously eligible + short streak would normally carry True — blocked must kill that.
    d = decide_check(CheckStats("emb_quality", 0.0, 0.0, blocked="corpus_too_small:8<720"), True, 0)
    assert d.status == "blocked" and d.eligible is False and d.short_streak == 0
    # A later insufficient epoch that sees prev=False (from the blocked decision) stays
    # ineligible — it may report status "carried" (carrying False) or "insufficient".
    d2 = decide_check(CheckStats("emb_quality", 10.0, 1.0), d.eligible, d.short_streak)
    assert d2.eligible is False
    # Non-empty blocked with large n_eff still loses (block wins over the numbers).
    d3 = decide_check(CheckStats("emb_quality", 500.0, 0.99, blocked="corpus_not_production:review_pending"), True, 1)
    assert d3.status == "blocked" and d3.eligible is False and d3.short_streak == 0


def test_blocked_tier_no_stage_progression_no_slot():
    """A blocked check keeps the whole tier ineligible: no stage advance, no slot fill."""
    from allocation_v3 import MinerEvidence, MinerTierEvidence, allocate_v3
    from v3_params import CHECKS_BY_TIER

    def tiers(blocked_emb=False):
        out = {}
        for tier, names in CHECKS_BY_TIER.items():
            checks = []
            for n in names:
                if blocked_emb and n == "emb_quality":
                    checks.append(CheckStats(n, 0.0, 0.0, blocked="corpus_too_small:8<720"))
                elif tier == "gen":
                    checks.append(CheckStats(n, 0.0, 0.0, blocked="generation_corpus_absent"))
                else:
                    checks.append(CheckStats(n, 200.0, 0.99))
            out[tier] = MinerTierEvidence(tuple(checks), 1.0, 400, 432,
                                          {"vdb_query": 0.0} if tier == "vdb" else None)
        return out

    demand = {k: 0.0 for k in ("vdb_storage", "vdb_query", "emb", "rerank", "gen")}
    # Epoch 1: emb blocked → emb stays observation, no emb holder.
    m = [MinerEvidence(1, "hk", "ck", 100, tiers(True))]
    r = allocate_v3(m, demand, None, 1000)
    assert r.state["stages"]["emb"]["hk"]["stage"] == "observation"
    assert "hk" not in r.holders.get("emb", [])  # holders is on the result
    assert "hk" not in r.eligible.get("emb", [])
    # Epoch 2 with emb unblocked: provisional starts (first eligibility).
    m2 = [MinerEvidence(1, "hk", "ck", 100, tiers(False))]
    r2 = allocate_v3(m2, demand, r.state, 2000)
    assert r2.state["stages"]["emb"]["hk"]["stage"] == "provisional"
    assert "hk" in r2.holders["emb"]


def test_slot_window_hours_override(monkeypatch):
    from v3_params import SLOT_RULE_WINDOW_HOURS, slot_window_hours
    assert slot_window_hours() == SLOT_RULE_WINDOW_HOURS
    monkeypatch.setenv("INCENTIVE_V3_SLOT_WINDOW_HOURS", "4")
    assert slot_window_hours() == 4
    monkeypatch.setenv("INCENTIVE_V3_SLOT_WINDOW_HOURS", "0")
    try:
        slot_window_hours()
        assert False, "expected error"
    except ValueError:
        pass
    monkeypatch.setenv("INCENTIVE_V3_SLOT_WINDOW_HOURS", "99")
    try:
        slot_window_hours()
        assert False, "expected error"
    except ValueError:
        pass
