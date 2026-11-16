"""Acceptance-sampling eligibility for incentive v3 (score_version 11).

Pure, offline. A check is eligible iff the decayed Kish n_eff >= n AND the decayed
pass rate p_hat >= c/n, where (n, c) is the smallest exact-binomial design meeting
both the false-ineligibility (FN) and false-eligibility (FP) budgets. Insufficient
evidence (n_eff < n) carries the previous decision for at most CARRY_EPOCHS epochs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from v3_params import CARRY_EPOCHS, CHECKS, CHECKS_BY_TIER


def _binom_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return math.fsum(math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k + 1))


@lru_cache(maxsize=None)
def design(p_good: float, p_bad: float, fn_max: float, fp_max: float, n_max: int = 3000) -> tuple[int, int]:
    """Smallest (n, c): P[Bin(n,p_good) < c] <= fn_max and P[Bin(n,p_bad) >= c] <= fp_max."""
    for n in range(1, n_max):
        for c in range(1, n + 1):
            if _binom_cdf(c - 1, n, p_good) > fn_max:
                break
            if 1.0 - _binom_cdf(c - 1, n, p_bad) <= fp_max:
                return n, c
    raise ValueError("no acceptance design")


def _verify_table() -> None:
    for name, (_, pg, pb, fn, fp, n, c) in CHECKS.items():
        if design(pg, pb, fn, fp) != (n, c):
            raise RuntimeError(f"v3 acceptance table drift for {name}")


_verify_table()


@dataclass(frozen=True, slots=True)
class CheckStats:
    """Numeric-only evidence for one check (web computes; validator decides).
    `blocked` names a web-side coverage block (corpus too small / not production);
    a blocked check always carries n_eff = p_hat = 0."""
    name: str
    n_eff: float
    p_hat: float
    blocked: str = ""


@dataclass(frozen=True, slots=True)
class CheckDecision:
    name: str
    eligible: bool
    status: str          # "pass" | "fail" | "carried" | "insufficient" | "blocked"
    short_streak: int


def decide_check(stats: CheckStats, prev_eligible: bool | None, short_streak: int,
                 carry: int = CARRY_EPOCHS) -> CheckDecision:
    if stats.name not in CHECKS:
        raise ValueError(f"unknown check {stats.name!r}")
    # Explicit coverage block (corpus too small / not production / scoring off): never
    # eligible, never carried, streak reset. A blocked check must not keep a prior
    # eligible flag alive or advance stages/slots.
    if stats.blocked:
        return CheckDecision(stats.name, False, "blocked", 0)
    if not (math.isfinite(stats.n_eff) and math.isfinite(stats.p_hat)):
        raise ValueError("non-finite evidence")
    if stats.n_eff < 0 or not (0.0 <= stats.p_hat <= 1.0):
        raise ValueError("evidence out of range")
    _, _, _, _, _, n, c = CHECKS[stats.name]
    if stats.n_eff >= n:
        ok = stats.p_hat >= c / n
        return CheckDecision(stats.name, ok, "pass" if ok else "fail", 0)
    if prev_eligible is not None and short_streak < carry:
        return CheckDecision(stats.name, bool(prev_eligible), "carried", short_streak + 1)
    return CheckDecision(stats.name, False, "insufficient", short_streak + 1)


def decide_tier(tier: str, stats: dict[str, CheckStats], prev: dict[str, tuple[bool | None, int]],
                carry: int = CARRY_EPOCHS) -> tuple[bool, dict[str, CheckDecision]]:
    """All checks of the tier must be eligible. A missing check counts as insufficient."""
    out: dict[str, CheckDecision] = {}
    for name in CHECKS_BY_TIER[tier]:
        s = stats.get(name, CheckStats(name, 0.0, 0.0))
        p_ok, streak = prev.get(name, (None, 0))
        out[name] = decide_check(s, p_ok, streak, carry)
    return all(d.eligible for d in out.values()), out
