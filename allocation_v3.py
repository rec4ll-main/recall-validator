"""Incentive v3 epoch allocator (score_version 11) -- pure and offline.

Per epoch (100 units of miner emission):
  * readiness pools R_t = budget_t * READINESS_SPLIT, K slots per tier, each slot worth
    R_t/K; a holder earns (R_t/K) * r * g (r = min-check decayed pass rate x latency x
    capacity factor, g = stage weight); everything not earned burns;
    that is readiness_mode="slots"; the default "shares" instead pays every tier-eligible
    miner R_t * q / max(sum q, K) with q = r * g (at most one slot's pay each while
    sum q <= K); the slot table still advances in both modes;
  * service pools P_s: f_s * P_s is paid among *tier-eligible* miners in proportion to
    credited service (stage-weighted), (1 - f_s) * P_s burns; f from the demand ramp;
  * the storage pool always burns while STORAGE_REWARDS_ENABLED is False;
  * per-miner cap max(10, 200/n_eligible) units, less a u16 rounding margin; overflow burns;
  * every burned unit goes to the owner/immune burn UID (resolved by the caller).
Serious failures (isolation breach, retention-proof mismatch) make the miner ineligible
in every tier for the epoch and reset its stage. No randomness, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from v3_acceptance import CheckStats, decide_tier
from v3_demand import ramp_step
from v3_params import (
    READINESS_SPLIT,
    readiness_pools,
    service_pools,
    DEMAND_W,
    PER_MINER_CAP_FLOOR,
    PER_MINER_CAP_NUMERATOR,
    READINESS_MODE_DEFAULT,
    READINESS_MODES,
    READINESS_POOL,
    SERVICE_POOLS,
    SLOTS_PER_TIER,
    STORAGE_REWARDS_ENABLED,
    TIERS,
    TOTAL_UNITS,
)
from v3_slots import DEFAULT_RULE, SlotRule, TierCandidate, advance_stage, stage_weight, update_slots

UNIT_TOL = 1e-9


@dataclass(frozen=True, slots=True)
class MinerTierEvidence:
    checks: tuple[CheckStats, ...]
    factor: float = 1.0          # latency x capacity factor in [0, 1]
    h_passes: int = 0            # binding-check trailing-H passes (slot rule)
    h_n: int = 0
    credited: dict | None = None # pool name -> credited service units for this miner


@dataclass(frozen=True, slots=True)
class MinerEvidence:
    uid: int
    hotkey: str
    coldkey: str
    registration_block: int
    tiers: dict                  # tier -> MinerTierEvidence
    serious_failure: bool = False


@dataclass(slots=True)
class V3Result:
    epoch_start_block: int
    units_by_uid: dict[int, float] = field(default_factory=dict)
    hotkey_by_uid: dict[int, str] = field(default_factory=dict)
    burn_units: float = 0.0
    burn_by_reason: dict[str, float] = field(default_factory=dict)
    # With burn_unearned=False: what would have burned, by reason, now paid to miners.
    redistributed_by_reason: dict[str, float] = field(default_factory=dict)
    eligible: dict[str, list[str]] = field(default_factory=dict)
    holders: dict[str, list[str]] = field(default_factory=dict)
    # readiness_mode="shares" only: tier -> hotkeys paid readiness this epoch.
    readiness_payees: dict[str, list[str]] = field(default_factory=dict)
    f: dict[str, float] = field(default_factory=dict)
    events: list = field(default_factory=list)
    state: dict = field(default_factory=dict)


def _dedup(miners: list[MinerEvidence]) -> list[MinerEvidence]:
    """One miner per coldkey: highest registration block, tie lowest uid (as v2)."""
    best: dict[str, MinerEvidence] = {}
    for m in miners:
        cur = best.get(m.coldkey)
        if cur is None or (m.registration_block, -m.uid) > (cur.registration_block, -cur.uid):
            best[m.coldkey] = m
    return sorted(best.values(), key=lambda m: m.uid)


def _unit(v) -> float:
    """Clamp to [0, 1]; NaN / inf / non-numbers -> 0 (never positive by default)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    f = float(v)
    if not math.isfinite(f):
        return 0.0
    return max(0.0, min(1.0, f))


def _add(d: dict, k, v: float) -> None:
    d[k] = d.get(k, 0.0) + v


U16_MAX = 65535


def _u16_safe_share(cap_share: float, n_rows: int) -> float:
    """Largest share that still decodes at or under ``cap_share`` after SDK u16 encoding.

    The SDK max-upscales, casts through float32 and rounds each row to u16, so every
    encoded row is off by under one unit of 65535 * s / s_max. With N rows the decoded
    share q_i / sum(q) moves by at most s_max * (1 + s_i * N) / 65535 <= (1 + cap * N) /
    65535. Subtracting that bound keeps a row at the cap from decoding above it. A cap
    of 1 (or more) needs no margin."""
    if cap_share >= 1.0:
        return 1.0
    return cap_share - (1.0 + cap_share * n_rows) / U16_MAX


def verifier_cap_units(n_paid: int) -> float:
    """Units a miner may hold so the bundle row check accepts it: the per-row miner cap
    max(0.10, 2/N) over the paid miner rows, less the u16 rounding margin (burn row
    counted in N for the margin)."""
    from allocation import CAP_FLOOR, CAP_TWO_OVER_N

    n = max(1, int(n_paid))
    share = min(1.0, max(CAP_FLOOR, CAP_TWO_OVER_N / n))
    return TOTAL_UNITS * _u16_safe_share(share, n + 1)


def water_fill(earned: dict[int, float], total: float, cap: float) -> tuple[dict[int, float], float]:
    """Spread ``total`` over the miners in proportion to ``earned``, at most ``cap`` each.

    Miners whose proportional share exceeds the cap are clamped to it and the excess
    is spread again over the rest, in proportion to what they earned, until no share
    exceeds the cap. A miner that earned more never gets less. Returns (units, left);
    ``left`` is positive only when every miner is clamped and the cap cannot absorb
    ``total``. Deterministic: uids are processed in ascending order."""
    free = sorted(u for u, v in earned.items() if v > 0)
    units: dict[int, float] = {}
    remaining = total
    while free:
        scale = remaining / math.fsum(earned[u] for u in free)
        over = [u for u in free if earned[u] * scale > cap]
        if not over:
            for u in free:
                units[u] = earned[u] * scale
            remaining = 0.0
            break
        for u in over:
            units[u] = cap
        remaining = total - math.fsum(units.values())
        free = [u for u in free if u not in units]
    left = remaining if remaining > UNIT_TOL else 0.0
    return units, left


def allocate_v3(miners: list[MinerEvidence], demand: dict[str, float], state: dict | None,
                epoch_start_block: int, rule: SlotRule = DEFAULT_RULE,
                k: int = SLOTS_PER_TIER, readiness_split: float = READINESS_SPLIT,
                burn_unearned: bool = True, readiness_mode: str = READINESS_MODE_DEFAULT) -> V3Result:
    """Advance the v3 state by one epoch and return units per uid + burn.

    readiness_split sets the readiness share of each tier budget (default 0.40, so 60%
    service). With burn_unearned=False, whatever would burn is paid to the miners in
    proportion to the units they earned, water-filled under the bundle row cap for the
    paid set (``verifier_cap_units``); the owner/burn UID gets weight only when no miner
    earned anything (or, never in practice, when the cap cannot absorb 100 units).
    Stages, slots and ramps are identical in both modes.

    readiness_mode="slots" pays readiness to the K slot holders of each tier. "shares"
    (default) pays every tier-eligible miner (eligible, stage weight > 0) the share
    R_t * q / max(sum q, K) with q = r * stage weight: no miner earns more than one slot
    would pay, and with more than K quality units the pool is split by quality. The
    slot table, stages and carry advance exactly as in slots mode either way, and
    ``holders`` keeps listing slot holders; payees are in ``readiness_payees``.

    Idempotent per epoch: calling again with the same epoch_start_block and the state
    returned by the first call raises (the caller must reuse the stored result)."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("slots per tier must be a positive integer")
    if readiness_mode not in READINESS_MODES:
        raise ValueError(f"readiness mode must be one of {', '.join(READINESS_MODES)}")
    st = {
        "last_epoch": None, "stages": {}, "carry": {}, "slots": {}, "ramps": {}, "identities": {},
    }
    if state:
        st.update({k_: state[k_] for k_ in st if k_ in state})
    if st["last_epoch"] is not None and epoch_start_block <= int(st["last_epoch"]):
        raise ValueError("v3 state already advanced for this or a later epoch")
    readiness_pool = readiness_pools(readiness_split)
    service_pool = service_pools(readiness_split)
    for name in service_pool:
        d = demand.get(name, 0.0)
        if not (isinstance(d, (int, float)) and math.isfinite(d) and d >= 0):
            raise ValueError(f"invalid credited demand for {name}")

    res = V3Result(epoch_start_block=epoch_start_block)
    cohort = _dedup(list(miners))
    if len({m.uid for m in cohort}) != len(cohort):
        raise ValueError("duplicate uid in v3 cohort")
    by_id = {m.hotkey: m for m in cohort}
    for m in cohort:
        res.hotkey_by_uid[m.uid] = m.hotkey

    # Identity = (hotkey, uid, registration block). A hotkey that was deregistered and
    # re-registered, or moved to another uid, restarts from observation: its stage,
    # carry, and slot holdings are dropped (replaced UIDs).
    prev_ids = st.get("identities") or {}
    new_ids = {m.hotkey: [int(m.uid), int(m.registration_block)] for m in cohort}
    reset = sorted(h for h, ident in new_ids.items() if h in prev_ids and list(prev_ids[h]) != ident)
    if reset:
        for tier in TIERS:
            for key in ("stages", "carry"):
                tier_map = dict(st[key].get(tier, {}))
                for h in reset:
                    tier_map.pop(h, None)
                st[key] = dict(st[key]); st[key][tier] = tier_map
            table = dict(st["slots"].get(tier, {}))
            table["holders"] = [h for h in table.get("holders", []) if h not in reset]
            table["streak"] = {h: v for h, v in (table.get("streak") or {}).items() if h not in reset}
            st["slots"] = dict(st["slots"]); st["slots"][tier] = table
        res.events += [{"epoch": epoch_start_block, "kind": "identity_reset", "identity": h} for h in reset]

    new_stages: dict = {}
    new_carry: dict = {}
    new_slots: dict = {}
    raw: dict[str, float] = {}       # hotkey -> units before cap
    for tier in TIERS:
        cands: dict[str, TierCandidate] = {}
        stages_t = dict(st["stages"].get(tier, {}))
        carry_t = dict(st["carry"].get(tier, {}))
        for m in cohort:
            ev = m.tiers.get(tier)
            stats = {c.name: c for c in ev.checks} if ev else {}
            prev = {n: tuple(v) for n, v in carry_t.get(m.hotkey, {}).items()}
            ok, dec = decide_tier(tier, stats, prev)
            if m.serious_failure:
                ok = False
            carry_t[m.hotkey] = {n: [d.eligible, d.short_streak] for n, d in dec.items()}
            stages_t[m.hotkey] = advance_stage(stages_t.get(m.hotkey), ok)
            if m.serious_failure:
                stages_t[m.hotkey] = {"stage": "observation", "prov_epochs": 0}
            r = 0.0
            if ok and ev and ev.checks:
                r = min(_unit(c.p_hat) for c in ev.checks) * _unit(ev.factor)
            cands[m.hotkey] = TierCandidate(m.hotkey, ok, r, int(ev.h_passes) if ev else 0,
                                            int(ev.h_n) if ev else 0)
        # identities that left the cohort lose state for the tier
        stages_t = {h: s for h, s in stages_t.items() if h in by_id}
        carry_t = {h: s for h, s in carry_t.items() if h in by_id}
        table, ev_list = update_slots(st["slots"].get(tier, {}), cands, stages_t, epoch_start_block, rule, k)
        new_stages[tier], new_carry[tier], new_slots[tier] = stages_t, carry_t, table
        res.events += [dict(e, tier=tier) for e in ev_list]
        res.holders[tier] = list(table["holders"])
        res.eligible[tier] = sorted(h for h, c in cands.items() if c.eligible and stage_weight(stages_t.get(h)) > 0)

        # readiness
        pool = readiness_pool[tier]
        paid = 0.0
        if readiness_mode == "shares":
            q = {h: cands[h].r * stage_weight(stages_t.get(h)) for h in res.eligible[tier]}
            denom = max(math.fsum(q.values()), float(k))
            payees = []
            for h in res.eligible[tier]:
                u = pool * q[h] / denom
                if u > 0:
                    _add(raw, h, u)
                    paid += u
                    payees.append(h)
            res.readiness_payees[tier] = payees
        else:
            per_slot = pool / k
            for h in table["holders"]:
                u = per_slot * cands[h].r * stage_weight(stages_t.get(h))
                if u > 0:
                    _add(raw, h, u)
                    paid += u
        if paid > pool + 1e-9:
            raise ValueError(f"readiness pool exceeded for {tier}")
        _add(res.burn_by_reason, f"readiness_unearned:{tier}", max(0.0, pool - paid))

    # service pools
    new_ramps: dict = {}
    for name, (tier, pool) in service_pool.items():
        ramp, f = ramp_step(st["ramps"].get(name), float(demand.get(name, 0.0)), DEMAND_W[name])
        new_ramps[name] = ramp
        res.f[name] = f
        if name == "vdb_storage" and not STORAGE_REWARDS_ENABLED:
            _add(res.burn_by_reason, "storage_disabled", pool)
            continue
        elig = res.eligible[tier]
        weights = {}
        for h in elig:
            g = stage_weight(new_stages[tier].get(h))
            ev = by_id[h].tiers.get(tier)
            cred = (ev.credited or {}).get(name) if ev else None
            # Missing credit is ZERO credit: a pool with demand but
            # no per-miner credited service burns instead of splitting by stage weight.
            if cred is None:
                weights[h] = 0.0
            elif isinstance(cred, (int, float)) and not isinstance(cred, bool) and math.isfinite(cred) and cred >= 0:
                weights[h] = g * float(cred)
            else:
                raise ValueError("invalid credited service units")
        tw = math.fsum(weights.values())
        paid_pool = pool * f
        if tw <= 0 or paid_pool <= 0:
            _add(res.burn_by_reason, f"service_unpaid:{name}", pool)
            continue
        paid = 0.0
        for h, w in weights.items():
            if w > 0:
                u = paid_pool * w / tw
                _add(raw, h, u)
                paid += u
        _add(res.burn_by_reason, f"service_unpaid:{name}", pool - paid)

    # per-miner cap
    # Kept strictly under the bundle row cap after u16 rounding: a miner exactly at the
    # floor would otherwise decode a hair above 0.10 and the bundle would be refused.
    n_elig = len({h for t in TIERS for h in res.eligible[t]})
    cap = max(PER_MINER_CAP_FLOOR, PER_MINER_CAP_NUMERATOR / max(n_elig, 1))
    n_paid = sum(1 for u in raw.values() if u > 0)
    cap = TOTAL_UNITS * _u16_safe_share(min(1.0, cap / TOTAL_UNITS), n_paid + 1)
    for h, u in raw.items():
        if u > cap:
            _add(res.burn_by_reason, "per_miner_cap", u - cap)
            u = cap
        if u > 0:
            res.units_by_uid[by_id[h].uid] = u
    res.burn_units = TOTAL_UNITS - math.fsum(res.units_by_uid.values())
    if res.burn_units < -UNIT_TOL:
        raise ArithmeticError("v3 allocation exceeds 100 units")
    res.burn_units = max(0.0, res.burn_units)
    if abs(math.fsum(res.burn_by_reason.values()) - res.burn_units) > 1e-6:
        raise ArithmeticError("v3 burn accounting mismatch")
    paid = math.fsum(res.units_by_uid.values())
    if not burn_unearned and paid > 0:
        # Pay everything to the miners in proportion to what they earned, but never above
        # the per-row cap the bundle check enforces for the paid set (a flat rescale let
        # a miner exceed it, and the master refused its own bundle). Water-filling moves
        # the excess to the unclamped miners; whatever no miner can take still burns.
        would_burn = res.burn_units
        units, left = water_fill(res.units_by_uid, TOTAL_UNITS, verifier_cap_units(len(res.units_by_uid)))
        res.units_by_uid = units
        kept = 1.0 - left / would_burn if would_burn > 0 else 1.0
        res.redistributed_by_reason = {r: v * kept for r, v in res.burn_by_reason.items()}
        res.burn_by_reason = {"verifier_cap": left} if left > 0 else {}
        res.burn_units = left
        if abs(math.fsum(res.units_by_uid.values()) + left - TOTAL_UNITS) > 1e-6:
            raise ArithmeticError("v3 redistribution does not sum to 100 units")
        if abs(math.fsum(res.redistributed_by_reason.values()) + left - would_burn) > 1e-6:
            raise ArithmeticError("v3 redistribution accounting mismatch")
    res.state = {"last_epoch": epoch_start_block, "stages": new_stages, "carry": new_carry,
                 "slots": new_slots, "ramps": new_ramps, "identities": new_ids}
    return res


def encode_v3(result: V3Result, burn_uid: int, burn_hotkey: str):
    """Encode units + burn row to the SDK u16 vector. Returns (uids, hotkeys, weights_u16)."""
    from allocation import normalize_uid_weight_pairs
    from allocation_encoding import convert_weights_and_uids, validate_encoding

    if burn_uid in result.units_by_uid:
        raise ValueError("burn uid collides with a paid miner")
    uids = list(result.units_by_uid) + [int(burn_uid)]
    shares = [result.units_by_uid[u] / TOTAL_UNITS for u in result.units_by_uid] + [result.burn_units / TOTAL_UNITS]
    hotkeys = [result.hotkey_by_uid[u] for u in result.units_by_uid] + [str(burn_hotkey)]
    pairs = [(u, s, h) for u, s, h in zip(uids, shares, hotkeys) if s > 0]
    if not pairs:
        raise ValueError("empty v3 vector")
    uids, shares, hotkeys = (list(x) for x in zip(*pairs))
    uids, shares, hotkeys = normalize_uid_weight_pairs(uids, shares, hotkeys)
    w_uids, w_vals = convert_weights_and_uids(uids, shares)
    w_uids, w_vals, _ = normalize_uid_weight_pairs(w_uids, w_vals)
    reason = validate_encoding(uids, w_uids, w_vals)
    if reason:
        raise ValueError(f"v3 encoding: {reason}")
    hk = dict(zip(uids, hotkeys))
    return list(w_uids), [hk[u] for u in w_uids], list(w_vals)
