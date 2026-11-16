"""Readiness stages and K-slot table with the harness-v2 challenger rule (v3).

Pure, offline, deterministic. State is a plain dict so it can be persisted as JSON.

Challenger rule (SLOT_RULE_KIND):
  "Z": the weakest holder is the one with the lowest undecayed pass rate on its
       binding (lowest-p) check over the trailing SLOT_RULE_WINDOW_HOURS; a FULL
       non-holder displaces it when the two-proportion z statistic
       (challenger - weakest) >= SLOT_RULE_Z on SLOT_RULE_CONFIRM_EPOCHS
       consecutive epochs.  At most MAX_DISPLACEMENTS_PER_EPOCH per tier per epoch.
  "M": legacy margin rule (r_challenger >= r_weakest + margin for k epochs); kept
       for comparison only, selected by passing kind="M".
Ineligible holders lose their slot immediately; free slots are filled best-r first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from v3_params import (
    MAX_DISPLACEMENTS_PER_EPOCH,
    PROVISIONAL_EPOCHS,
    PROVISIONAL_WEIGHT,
    SLOT_RULE_CONFIRM_EPOCHS,
    SLOT_RULE_KIND,
    SLOT_RULE_Z,
    SLOTS_PER_TIER,
)

STAGE_OBSERVATION = "observation"
STAGE_PROVISIONAL = "provisional"
STAGE_FULL = "full"


@dataclass(frozen=True, slots=True)
class SlotRule:
    kind: str = SLOT_RULE_KIND
    z: float = SLOT_RULE_Z
    confirm: int = SLOT_RULE_CONFIRM_EPOCHS
    margin: float = 0.10
    margin_epochs: int = 5


DEFAULT_RULE = SlotRule()


@dataclass(frozen=True, slots=True)
class TierCandidate:
    identity: str          # coldkey-deduped identity (hotkey of the cohort survivor)
    eligible: bool
    r: float               # min over checks of decayed p_hat (x latency x capacity)
    h_passes: int          # trailing-H undecayed passes on the binding check
    h_n: int               # trailing-H undecayed probes on the binding check


def advance_stage(stage: dict, eligible: bool) -> dict:
    """stage = {"stage": str, "prov_epochs": int}. Returns a new dict."""
    s = dict(stage) if stage else {"stage": STAGE_OBSERVATION, "prov_epochs": 0}
    if not eligible:
        if s["stage"] == STAGE_PROVISIONAL:
            s = {"stage": STAGE_OBSERVATION, "prov_epochs": 0}
        return s
    if s["stage"] == STAGE_OBSERVATION:
        return {"stage": STAGE_PROVISIONAL, "prov_epochs": 1}
    if s["stage"] == STAGE_PROVISIONAL:
        n = s["prov_epochs"] + 1
        return {"stage": STAGE_FULL if n >= PROVISIONAL_EPOCHS else STAGE_PROVISIONAL, "prov_epochs": n}
    return s


def stage_weight(stage: dict | None) -> float:
    if not stage:
        return 0.0
    return {STAGE_FULL: 1.0, STAGE_PROVISIONAL: PROVISIONAL_WEIGHT}.get(stage["stage"], 0.0)


def z_stat(a: tuple[int, int], b: tuple[int, int]) -> float:
    (pa, na), (pb, nb) = a, b
    if na <= 0 or nb <= 0:
        return 0.0
    pp = (pa + pb) / (na + nb)
    se = math.sqrt(max(pp * (1.0 - pp), 1e-9) * (1.0 / na + 1.0 / nb))
    return (pa / na - pb / nb) / se


def _rate(c: TierCandidate) -> float:
    return c.h_passes / c.h_n if c.h_n > 0 else 0.0


def update_slots(table: dict, cands: dict[str, TierCandidate], stages: dict[str, dict],
                 epoch: int, rule: SlotRule = DEFAULT_RULE, k: int = SLOTS_PER_TIER) -> tuple[dict, list]:
    """table = {"holders": [...], "streak": {id: n}}. Returns (new_table, events).

    events: list of dicts {"epoch","kind": "fill"|"drop"|"shrink"|"displace", ...}.
    A table carried from a larger k is trimmed to k first ("shrink"); see below.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("slots per tier must be a positive integer")
    holders = list(table.get("holders", []))
    streak_prev = dict(table.get("streak", {}))
    events: list[dict] = []
    ok = {i for i, c in cands.items() if c.eligible and c.r > 0 and stage_weight(stages.get(i)) > 0}
    for h in list(holders):
        if h not in ok:
            holders.remove(h)
            events.append({"epoch": epoch, "kind": "drop", "identity": h})
    if len(holders) > k:
        # The slot count was reduced on a running state. Keep the k strongest holders
        # by the same ranking the displacement rule uses for its weakest holder
        # (binding-check pass rate for Z, readiness r otherwise), ties by r, then
        # identity; release the rest. Deterministic, so every replay agrees.
        def strength(h):
            primary = _rate(cands[h]) if rule.kind == "Z" else cands[h].r
            return (-primary, -cands[h].r, h)
        keep = sorted(holders, key=strength)[:k]
        for h in [h for h in holders if h not in keep]:
            holders.remove(h)
            events.append({"epoch": epoch, "kind": "shrink", "identity": h})
    free = sorted((i for i in ok if i not in holders), key=lambda i: (-cands[i].r, i))
    while len(holders) < k and free:
        i = free.pop(0)
        holders.append(i)
        events.append({"epoch": epoch, "kind": "fill", "identity": i})
    streak: dict[str, int] = {}
    if len(holders) >= k and free:
        if rule.kind == "Z":
            weakest = min(holders, key=lambda h: (_rate(cands[h]), h))
        else:
            weakest = min(holders, key=lambda h: (cands[h].r, h))
        for c in free:
            if (stages.get(c) or {}).get("stage") != STAGE_FULL:
                continue
            if rule.kind == "Z":
                cond = z_stat((cands[c].h_passes, cands[c].h_n), (cands[weakest].h_passes, cands[weakest].h_n)) >= rule.z
            else:
                cond = cands[c].r >= cands[weakest].r + rule.margin - 1e-12
            if cond:
                streak[c] = streak_prev.get(c, 0) + 1
        need = rule.confirm if rule.kind == "Z" else rule.margin_epochs
        done = 0
        for c in sorted(streak, key=lambda c: (-cands[c].r, c)):
            if done >= MAX_DISPLACEMENTS_PER_EPOCH:
                break
            if streak[c] >= need:
                holders.remove(weakest)
                holders.append(c)
                events.append({"epoch": epoch, "kind": "displace", "identity": c, "displaced": weakest})
                streak.pop(c)
                done += 1
    return {"holders": holders, "streak": streak}, events
