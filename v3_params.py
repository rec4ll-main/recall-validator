"""Incentive v3 (score_version 11) parameters -- single source of truth.

Every value here is a CANDIDATE setting from the incentive-v3 launch profile and
the synthetic harness rerun; score_version 11 is the only scoring path. Units: the
subnet's miner emission per epoch is 100 units; whatever is not paid to a miner goes
to the owner/burn UID.
"""

from __future__ import annotations

V3_SCORE_VERSION = "11"
V3_SCORE_KIND = "readiness_service_v3"
V3_METRICS_SCHEMA = "recall-v3-metrics-v1"
V3_EVIDENCE_WINDOW_SECONDS = 21600          # 6 h readiness window
V3_HALF_LIFE_SECONDS = 10800                # 3 h decay half-life

TIERS = ("vdb", "emb", "rerank", "gen")
TOTAL_UNITS = 100.0

# Tier budgets (the same 30/22/18/30 split score_version 10 used).
TIER_BUDGET = {"vdb": 30.0, "emb": 22.0, "rerank": 18.0, "gen": 30.0}

# CANDIDATE: 40 % readiness / 60 % delivered service.
READINESS_SPLIT = 0.40
READINESS_POOL = {t: TIER_BUDGET[t] * READINESS_SPLIT for t in TIERS}  # 12 / 8.8 / 7.2 / 12

# How a tier's readiness pool is paid (INCENTIVE_V3_READINESS_MODE):
#   shares -> every tier-eligible miner, R_t * quality / max(sum quality, K) (default);
#   slots  -> the SLOTS_PER_TIER slot holders, R_t/K * quality each (pre-shares behaviour).
READINESS_MODES = ("slots", "shares")
READINESS_MODE_DEFAULT = "shares"

# CANDIDATE: VDB 12 readiness / 12 storage / 6 query.
SERVICE_POOLS = {
    "vdb_storage": ("vdb", 12.0),
    "vdb_query": ("vdb", 6.0),
    "emb": ("emb", 13.2),
    "rerank": ("rerank", 10.8),
    "gen": ("gen", 18.0),
}



def readiness_pools(split: float) -> dict:
    """Readiness pool per tier for a readiness share in [0, 1]."""
    if not (0.0 <= split <= 1.0):
        raise ValueError("readiness split must be in [0, 1]")
    if split == READINESS_SPLIT:
        return dict(READINESS_POOL)
    return {t: TIER_BUDGET[t] * split for t in TIERS}


def service_pools(split: float) -> dict:
    """Service pools for a readiness share in [0, 1]; VDB service keeps storage:query at 2:1."""
    if not (0.0 <= split <= 1.0):
        raise ValueError("readiness split must be in [0, 1]")
    if split == READINESS_SPLIT:
        return dict(SERVICE_POOLS)
    s = 1.0 - split
    return {
        "vdb_storage": ("vdb", TIER_BUDGET["vdb"] * s * 2.0 / 3.0),
        "vdb_query": ("vdb", TIER_BUDGET["vdb"] * s / 3.0),
        "emb": ("emb", TIER_BUDGET["emb"] * s),
        "rerank": ("rerank", TIER_BUDGET["rerank"] * s),
        "gen": ("gen", TIER_BUDGET["gen"] * s),
    }


# Storage retention proof is not proven end-to-end: the storage pool ALWAYS burns.
# Deliberately a code constant, not an env flag (enabling it needs a reviewed PR).
STORAGE_REWARDS_ENABLED = False

# Demand normalizers W (credited units per epoch at which f reaches 1). CANDIDATE.
DEMAND_W = {
    "vdb_query": 1000.0,
    "emb": 2000.0,
    "rerank": 1000.0,
    "gen": 500.0,
    "vdb_storage": 1_000_000.0,
}
DEMAND_HALF_LIFE_EPOCHS = 10
DEMAND_LAMBDA = 1.0 - 2.0 ** (-1.0 / DEMAND_HALF_LIFE_EPOCHS)  # 0.0670
DEMAND_DELTA_UP = 0.25

# Per-miner cap (units) = max(10, 200 / n_eligible); overflow burns.
PER_MINER_CAP_FLOOR = 10.0
PER_MINER_CAP_NUMERATOR = 200.0

# Readiness slots (K=4) and new-miner stages.
SLOTS_PER_TIER = 4
PROVISIONAL_WEIGHT = 0.5
PROVISIONAL_EPOCHS = 5
MAX_DISPLACEMENTS_PER_EPOCH = 1

# Challenger rule. See SLOT_RULE in v3_slots.py for the
# chosen rule and the dual targets it was selected against.
# Selected on exploration seeds, confirmed on fresh seeds:
# Z(z=3.0, H=48 h, c=3) is the only grid rule meeting BOTH targets on all four tiers:
#   bad swaps <= 1 per 1000 tier-epochs, and P(+0.05 challenger wins within 40 epochs) >= 0.95.
# The v3 rule (margin 0.05 x 2 epochs) is kept only as SlotRule(kind="M") for comparison.
SLOT_RULE_KIND = "Z"           # two-proportion z-test on trailing undecayed counts
SLOT_RULE_Z = 3.0
SLOT_RULE_WINDOW_HOURS = 48

def slot_window_hours() -> int:
    """Trailing undecayed-count window for the Z slot rule.

    Default 48 (production), set by INCENTIVE_V3_SLOT_WINDOW_HOURS. Must match the web's
    INCENTIVE_V3_SLOT_WINDOW_SECONDS / 3600. Never use a reduced window on mainnet.
    """
    import os
    raw = (os.environ.get("INCENTIVE_V3_SLOT_WINDOW_HOURS") or "").strip()
    if not raw:
        return SLOT_RULE_WINDOW_HOURS
    v = int(raw)
    if v <= 0 or v > SLOT_RULE_WINDOW_HOURS:
        raise ValueError(
            f"INCENTIVE_V3_SLOT_WINDOW_HOURS must be in 1..{SLOT_RULE_WINDOW_HOURS}, got {v}"
        )
    return v

SLOT_RULE_CONFIRM_EPOCHS = 3

# Carry-over: insufficient evidence (validator under-delivery) keeps the previous
# per-check decision for at most this many epochs. Miner faults are failures, not gaps.
CARRY_EPOCHS = 2

# Acceptance-sampling designs (spec v3 sec 3.3). (n, c) are recomputed from the
# exact binomial in v3_acceptance and asserted equal at import time.
# name: (tier, p_good, p_bad, fn_budget, fp_budget, n, c)
CHECKS = {
    "vdb_integrity": ("vdb", 0.99, 0.90, 0.005, 0.05, 89, 85),
    "vdb_retention": ("vdb", 0.99, 0.90, 0.005, 0.05, 89, 85),
    "emb_quality": ("emb", 0.95, 0.75, 0.01, 0.05, 45, 39),
    "rerank_quality": ("rerank", 0.90, 0.70, 0.01, 0.05, 57, 46),
    "gen_qa": ("gen", 0.85, 0.60, 0.0025, 0.05, 60, 43),
    "gen_citation": ("gen", 0.85, 0.60, 0.0025, 0.05, 60, 43),
    "gen_abstention": ("gen", 0.85, 0.60, 0.0025, 0.05, 60, 43),
    "gen_injection": ("gen", 0.98, 0.85, 0.0025, 0.05, 68, 63),
}
CHECKS_BY_TIER = {t: tuple(k for k, v in CHECKS.items() if v[0] == t) for t in TIERS}

# Probe quotas per miner per hour (stratified schedule; web side enforces).
PROBE_QUOTA_PER_HOUR = {
    "vdb_integrity": 36, "vdb_retention": 36, "emb_quality": 12, "rerank_quality": 16,
    "gen_qa": 16, "gen_citation": 16, "gen_abstention": 16, "gen_injection": 18,
}
