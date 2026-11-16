"""allocate_v3 output always passes the bundle row check (both burn modes).

Regression: with burn_unearned=False a flat rescale to 100 units pushed miners over the
v10 per-row cap max(0.10, 2/N) that ``validate_weight_rows`` enforces, so the master
refused its own bundle; and a miner exactly at the 10-unit floor decoded a hair above
0.10 after u16 rounding. Runs the real allocate_v3 -> encode_v3 -> signed_burn_uid ->
validate_weight_rows chain. The SDK u16 codec is used when importable, else an offline
stand-in with the same semantics (float32 cast, max-upscale, round to u16, drop zeros).
"""

from __future__ import annotations

import math
import struct
import types

import pytest

import allocation_encoding
import allocation_v3
import bundle_weights
import owner_burn
from allocation_v3 import MinerEvidence, MinerTierEvidence, allocate_v3, encode_v3, water_fill
from bundle_weights import miner_cap, signed_burn_uid, validate_weight_rows
from v3_acceptance import CheckStats
from v3_params import CHECKS, CHECKS_BY_TIER, DEMAND_W, TIERS, TOTAL_UNITS

BURN_UID, BURN_HK = 0, "5owner"
FULL = {"stage": "full", "prov_epochs": 5}
NEED = max(c[6] / c[5] for c in CHECKS.values())  # strictest pass ratio
NEFF = max(c[5] for c in CHECKS.values()) * 2
SIZES = (3, 5, 6, 8, 10, 12, 20, 50)
TOL = 1e-6


def _f32(x):
    return struct.unpack("f", struct.pack("f", float(x)))[0]


def _offline_codec(uids, weights):
    w = [_f32(v) for v in weights]
    if sum(w) == 0:
        return [], []
    m = float(max(w))
    out_u, out_v = [], []
    for u, v in zip(uids, w):
        q = round(float(_f32(float(v) / m)) * 65535)
        if q:
            out_u.append(int(u))
            out_v.append(q)
    return out_u, out_v


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(owner_burn, "resolve_owner_burn_uid", lambda *a, **k: (BURN_UID, BURN_HK))
    try:
        from bittensor.utils.weight_utils import convert_and_normalize_weights_and_uids  # noqa: F401
    except ImportError:
        monkeypatch.setattr(allocation_encoding, "convert_weights_and_uids", _offline_codec)
        monkeypatch.setattr(bundle_weights, "convert_weights_and_uids", _offline_codec)


def _miner(uid, tiers_p, credit=100.0):
    return MinerEvidence(
        uid=uid, hotkey=f"5m{uid}", coldkey=f"5c{uid}", registration_block=1000 + uid,
        tiers={t: MinerTierEvidence(checks=tuple(CheckStats(n, NEFF, p) for n in CHECKS_BY_TIER[t]),
                                    factor=1.0, h_passes=95, h_n=100,
                                    credited={name: credit for name in ("vdb_query", "emb", "rerank", "gen")})
               for t, p in tiers_p.items()})


def _state(miners, demand_on):
    st = {"last_epoch": 100, "stages": {}, "carry": {}, "slots": {},
          "identities": {m.hotkey: [m.uid, m.registration_block] for m in miners},
          "ramps": {n: {"L": 10 * w, "f": 1.0} for n, w in DEMAND_W.items()} if demand_on else {}}
    for t in TIERS:
        hs = [m.hotkey for m in miners if t in m.tiers]
        st["stages"][t] = {h: dict(FULL) for h in hs}
        st["slots"][t] = {"holders": hs[:4], "streak": {}}
    return st


def _p(i):  # varied p_hat, all above the strictest pass ratio
    return min(1.0, NEED + (1 - NEED) * (0.3 + 0.7 * ((i * 37) % 10) / 10))


def _cohort(kind, n):
    if kind == "one_4tier_rest_1tier":
        return [_miner(1, {t: _p(1) for t in TIERS})] + [_miner(i, {"vdb": _p(i)}) for i in range(2, n + 1)]
    if kind == "half_4tier_half_1tier":
        return [_miner(i, {t: _p(i) for t in TIERS} if i % 2 else {"vdb": _p(i)}) for i in range(1, n + 1)]
    assert kind == "all_4tier_varied_p"
    return [_miner(i, {t: _p(i + k) for k, t in enumerate(TIERS)}) for i in range(1, n + 1)]


def _allocate(miners, demand_on, burn_unearned):
    demand = {n: 10 * w for n, w in DEMAND_W.items()} if demand_on else {}
    return allocate_v3(miners, demand, _state(miners, demand_on), 200, burn_unearned=burn_unearned)


def _check_rows(res):
    uids, hks, w = encode_v3(res, BURN_UID, BURN_HK)
    bu, _ = signed_burn_uid(None, None, 31, types.SimpleNamespace(uids=uids, hotkeys=hks))
    return uids, w, bu, validate_weight_rows(uids, w, native_cap=1.0, burn_uid=bu)


KINDS = ("one_4tier_rest_1tier", "half_4tier_half_1tier", "all_4tier_varied_p")


@pytest.mark.parametrize("demand_on", [True, False])
@pytest.mark.parametrize("burn_unearned", [True, False])
@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("kind", KINDS)
def test_allocation_passes_bundle_row_check(kind, n, burn_unearned, demand_on):
    res = _allocate(_cohort(kind, n), demand_on, burn_unearned)
    uids, w, bu, reason = _check_rows(res)
    assert reason is None
    total = sum(w)
    cap = miner_cap(1.0, uids, bu)
    assert all(v / total <= cap for u, v in zip(uids, w) if u != bu)  # no tolerance needed
    assert math.isclose(math.fsum(res.burn_by_reason.values()), res.burn_units, abs_tol=TOL)
    assert math.isclose(math.fsum(res.units_by_uid.values()) + res.burn_units, TOTAL_UNITS, abs_tol=TOL)


@pytest.mark.parametrize("demand_on", [True, False])
@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("kind", KINDS)
def test_no_burn_pays_100_units_and_preserves_order(kind, n, demand_on):
    miners = _cohort(kind, n)
    earned = _allocate(miners, demand_on, True)
    paid = _allocate(miners, demand_on, False)
    assert paid.burn_units == 0.0 and paid.burn_by_reason == {}
    assert math.isclose(math.fsum(paid.units_by_uid.values()), TOTAL_UNITS, abs_tol=TOL)
    assert math.isclose(math.fsum(paid.redistributed_by_reason.values()), earned.burn_units, abs_tol=TOL)
    assert set(paid.units_by_uid) == set(earned.units_by_uid)
    for a, ea in earned.units_by_uid.items():
        for b, eb in earned.units_by_uid.items():
            if ea > eb:
                assert paid.units_by_uid[a] >= paid.units_by_uid[b] - 1e-12
    uids, _, bu, _ = _check_rows(paid)
    assert BURN_UID not in uids and bu is None
    assert paid.state == earned.state
    again = _allocate(miners, demand_on, False)
    assert again.units_by_uid == paid.units_by_uid  # deterministic


def test_flat_rescale_would_have_been_refused():
    """The pre-fix rescale (TOTAL/paid on earned units) exceeds the row cap here."""
    res = _allocate(_cohort("one_4tier_rest_1tier", 5), True, True)
    scale = TOTAL_UNITS / math.fsum(res.units_by_uid.values())
    res.units_by_uid = {u: v * scale for u, v in res.units_by_uid.items()}
    res.burn_units, res.burn_by_reason = 0.0, {}
    assert _check_rows(res)[3] == "decoded share exceeds cap"
    assert _check_rows(_allocate(_cohort("one_4tier_rest_1tier", 5), True, False))[3] is None


def test_water_fill_clamps_and_redistributes_in_proportion():
    units, left = water_fill({1: 50.0, 2: 10.0, 3: 5.0, 4: 5.0}, 100.0, 40.0)
    assert left == 0.0 and units[1] == 40.0
    assert math.isclose(units[2], 30.0) and math.isclose(units[3], 15.0) and math.isclose(units[4], 15.0)
    # cascade: the second miner hits the cap after the first one's excess moves
    units, left = water_fill({1: 50.0, 2: 30.0, 3: 10.0, 4: 10.0}, 100.0, 35.0)
    assert units[1] == units[2] == 35.0 and math.isclose(units[3], 15.0) and left == 0.0


def test_water_fill_infeasible_cap_leaves_remainder():
    units, left = water_fill({1: 3.0, 2: 2.0, 3: 1.0}, 100.0, 20.0)
    assert units == {1: 20.0, 2: 20.0, 3: 20.0} and math.isclose(left, 40.0)


def test_no_burn_burns_what_the_cap_cannot_absorb(monkeypatch):
    """Guard path (unreachable with the real cap, since N * max(0.1, 2/N) >= 2): the
    remainder burns instead of exceeding the cap, and the accounting still sums."""
    monkeypatch.setattr(allocation_v3, "verifier_cap_units", lambda n: 30.0)
    miners = _cohort("half_4tier_half_1tier", 3)
    earned = _allocate(miners, True, True)
    assert math.fsum(earned.units_by_uid.values()) < 30.0 * len(earned.units_by_uid)
    res = _allocate(miners, True, False)
    assert all(v <= 30.0 for v in res.units_by_uid.values())
    assert math.isclose(res.burn_units, TOTAL_UNITS - 30.0 * len(res.units_by_uid), abs_tol=TOL)
    assert res.burn_by_reason == {"verifier_cap": res.burn_units}
    assert math.isclose(math.fsum(res.redistributed_by_reason.values()) + res.burn_units,
                        earned.burn_units, abs_tol=TOL)
