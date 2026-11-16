"""Incentive v3 runtime: the score_version 11 epoch allocation and envelope build.

This is the only scoring and weights path. Every epoch the master fetches v3 evidence,
advances the persisted allocator state, writes the epoch record (inputs digest, t_call,
units, burn, encoded vector) and signs a score_version 11 envelope from it. Startup
(main.enforce_v3_startup_guards) requires actual genesis, signer/permit and
process-lock checks first.

Allocator state and epoch records live under BUNDLE_STATE_DIR/v3/weights/ (the
``weights`` namespace name is kept so existing state is reused unchanged).
"""

from __future__ import annotations

import logging

from allocation_snapshot import build_snapshot, snapshot_digest
from allocation_v3 import allocate_v3, encode_v3
from v3_metrics_client import fetch_v3_metrics, to_miner_evidence
from v3_eligibility import EligibilityScopeError, bind_eligibility_artifact, eligibility_state_dir
from v3_params import (
    READINESS_MODE_DEFAULT, V3_EVIDENCE_WINDOW_SECONDS, V3_SCORE_KIND, V3_SCORE_VERSION,
)
from v3_state import (
    cleanup_tmp, digest, load_epoch_record, load_state, roll_forward_state, save_state,
    write_epoch_record,
)

logger = logging.getLogger(__name__)

# On-disk state namespace (v3_state / v3_eligibility). Fixed: renaming it would
# orphan the persisted allocator state of every running master.
V3_STATE_MODE = "weights"
# Persisted in every state file and epoch record; always empty. Kept so existing state
# and records stay byte-compatible, and a non-empty stored value (state written by a
# retired benchmark scope) is refused rather than reused.
_POLICY_FIELD = "generation_benchmark_policy_sha256"


def compute_v3_epoch(subtensor, metagraph, wallet, cfg, schedule, *, fetch=None,
                     resolve_burn=None):
    """Return (record, snapshot, digest). Persists state + record once per epoch."""
    mode = V3_STATE_MODE
    signer = wallet.hotkey
    snapshot = build_snapshot(subtensor, metagraph, cfg.netuid, None, cfg.genesis_hash,
                              block=schedule.epoch_start_block)
    # build_snapshot has proved actual block-0 genesis against cfg.genesis_hash.
    state_dir = eligibility_state_dir(cfg.state_dir, mode, cfg.v3_eligibility_scope)
    cleanup_tmp(state_dir, mode)
    roll_forward_state(state_dir, mode)
    existing = load_epoch_record(state_dir, mode, schedule.epoch_start_block)
    state = load_state(state_dir, mode)
    if state and state.get(_POLICY_FIELD, ""):
        raise EligibilityScopeError("stored v3 eligibility policy mismatch")
    snap_digest = snapshot_digest(snapshot)
    if existing is not None:
        if existing.get(_POLICY_FIELD, ""):
            raise EligibilityScopeError("stored v3 epoch policy mismatch")
        if existing.get("inputs", {}).get("generation_benchmark") is not None:
            raise EligibilityScopeError("stored v3 epoch carries benchmark evidence")
        if existing.get("snapshot_digest") != snap_digest:
            raise RuntimeError("stored v3 epoch record does not match the snapshot")
        return existing, snapshot, snap_digest
    cohort_full = [c for c in snapshot.cohort if c[1] != signer.ss58_address]
    cohort = [(uid, hk) for uid, hk, _, _ in cohort_full]
    fetch_fn = fetch if fetch is not None else fetch_v3_metrics
    ev = fetch_fn(cfg.api_url, cfg.internal_api_key, cohort, cfg.netuid,
                  schedule.epoch_start_block, allow_plaintext=cfg.allow_plaintext)
    miners = to_miner_evidence(ev, cohort_full)
    if resolve_burn is None:
        from owner_burn import resolve_owner_burn_uid as resolve_burn
    burn_uid, burn_hotkey = resolve_burn(subtensor, metagraph, cfg.netuid)
    if burn_uid is None or not burn_hotkey:
        raise RuntimeError("owner/burn UID unresolved; v3 fails closed")
    if state and state.get("last_epoch") is not None and int(state["last_epoch"]) >= schedule.epoch_start_block:
        # state already advanced past this epoch but its record is missing: never recompute
        # (that would double-advance stages/ramps); fail closed for this epoch.
        raise RuntimeError("v3 state is ahead of this epoch and its record is missing")
    readiness_mode = str(getattr(cfg, "v3_readiness_mode", READINESS_MODE_DEFAULT))
    result = allocate_v3(miners, ev.demand, state, schedule.epoch_start_block,
                         readiness_split=float(getattr(cfg, "v3_readiness_split", 0.40)),
                         burn_unearned=bool(getattr(cfg, "v3_burn_unearned", True)),
                         readiness_mode=readiness_mode)
    result.state[_POLICY_FIELD] = ""
    uids, hotkeys, weights_u16 = encode_v3(result, burn_uid, burn_hotkey)
    inputs = {
        "t_call": ev.t_call,
        "demand": ev.demand,
        "miners": [
            {"uid": m.uid, "hotkey": m.hotkey, "serious_failure": m.serious_failure,
             "tiers": {t: {"factor": e.factor, "h_passes": e.h_passes, "h_n": e.h_n,
                           "credited": e.credited,
                           "checks": [[c.name, c.n_eff, c.p_hat] + ([c.blocked] if c.blocked else [])
                                      for c in e.checks]}
                       for t, e in sorted(m.tiers.items())}}
            for m in miners
        ],
    }
    record = {
        "mode": mode,
        _POLICY_FIELD: "",
        "score_version": V3_SCORE_VERSION,
        "netuid": cfg.netuid,
        "epoch_start_block": schedule.epoch_start_block,
        "snapshot_digest": snap_digest,
        "t_call": ev.t_call,
        "inputs_digest": digest(inputs),
        "inputs": inputs,
        "units_by_uid": {str(u): v for u, v in sorted(result.units_by_uid.items())},
        "burn_uid": burn_uid,
        "burn_units": result.burn_units,
        "burn_by_reason": result.burn_by_reason,
        "redistributed_by_reason": result.redistributed_by_reason,
        "readiness_split": float(getattr(cfg, "v3_readiness_split", 0.40)),
        "burn_unearned": bool(getattr(cfg, "v3_burn_unearned", True)),
        "f": result.f,
        "holders": result.holders,
        "events": result.events,
        "uids": uids,
        "hotkeys": hotkeys,
        "weights_u16": weights_u16,
        "state_after": result.state,
    }
    # Recorded explicitly in every record (outside inputs_digest, like readiness_split:
    # the digest covers fetched evidence only). Records written before this setting
    # existed carry no readiness_mode and were computed with slots.
    record["readiness_mode"] = readiness_mode
    if readiness_mode == "shares":
        record["readiness_payees"] = result.readiness_payees
    # Record first (exclusive create), then state: a crash between the two leaves the
    # record, and the next call returns it without advancing state twice.
    write_epoch_record(state_dir, mode, schedule.epoch_start_block, record)
    save_state(state_dir, mode, result.state)
    return record, snapshot, snap_digest


def build_envelope_v3(subtensor, metagraph, wallet, cfg, schedule, *, fetch=None, resolve_burn=None):
    """Sign the score_version 11 envelope for this epoch's v3 allocation."""
    from bundle_v2 import sign_bundle

    record, snapshot, snap_digest = compute_v3_epoch(subtensor, metagraph, wallet, cfg, schedule,
                                                     fetch=fetch, resolve_burn=resolve_burn)
    payload = {
        "schema": "recall-allocation-bundle-v2",
        "genesis_hash": cfg.genesis_hash,
        "netuid": cfg.netuid,
        "master_hotkey": cfg.master_hotkey,
        "epoch_start_block": schedule.epoch_start_block,
        "subnet_epoch_index": schedule.subnet_epoch_index,
        "selection_block": schedule.cutoff_block,
        "anchor_block": snapshot.anchor_block,
        "anchor_hash": snapshot.anchor_hash,
        "score_version": V3_SCORE_VERSION,
        "score_kind": V3_SCORE_KIND,
        "evidence_window_seconds": V3_EVIDENCE_WINDOW_SECONDS,
        "snapshot_digest": snap_digest,
        "uids": record["uids"],
        "hotkeys": record["hotkeys"],
        "weights_u16": record["weights_u16"],
    }
    envelope = sign_bundle(payload, wallet.hotkey)
    bind_eligibility_artifact(cfg, envelope)
    return envelope
