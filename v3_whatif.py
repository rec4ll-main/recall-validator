"""Offline what-if: recompute a stored v3 epoch under slots and shares readiness.

Usage (read-only; no network, no chain, no writes):

    python3 -m v3_whatif <epochs/EPOCH.json> [--prev-record <epochs/PREV.json>] [--json]

The runtime advances the allocator from the state saved after the previous epoch, which
is the previous epoch record's ``state_after``. Without ``--prev-record`` the newest
record with a lower ``epoch_start_block`` in the same directory is used; with none, the
epoch is recomputed from an empty state (first epoch). Readiness split and burn mode are
taken from the record. Records do not store coldkeys: the miners kept after coldkey
dedup are the ones listed in the record's ``state_after.identities``, so the cohort is
restricted to those and needs no further dedup. Records written before
INCENTIVE_V3_READINESS_MODE existed carry no ``readiness_mode`` and were slots.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from allocation_v3 import MinerEvidence, MinerTierEvidence, allocate_v3
from v3_acceptance import CheckStats
from v3_params import READINESS_MODES, READINESS_SPLIT, TOTAL_UNITS


def load_record(path) -> dict:
    return json.loads(Path(path).read_text())


def find_prev_record(path) -> Path | None:
    """Newest record in the same directory with a lower epoch_start_block."""
    p = Path(path)
    epoch = int(load_record(p)["epoch_start_block"])
    older = sorted(int(f.stem) for f in p.parent.glob("*.json") if f.stem.isdigit() and int(f.stem) < epoch)
    return p.parent / f"{older[-1]}.json" if older else None


def miners_from_record(record: dict) -> list[MinerEvidence]:
    idents = (record.get("state_after") or {}).get("identities")
    out = []
    for m in record["inputs"]["miners"]:
        hk = m["hotkey"]
        if idents is not None and hk not in idents:
            continue  # dropped by coldkey dedup when the epoch ran
        reg = int(idents[hk][1]) if idents is not None else 0
        tiers = {}
        for t, e in m["tiers"].items():
            checks = tuple(CheckStats(c[0], c[1], c[2], c[3] if len(c) > 3 else "") for c in e["checks"])
            tiers[t] = MinerTierEvidence(checks, e.get("factor", 1.0), int(e.get("h_passes", 0)),
                                         int(e.get("h_n", 0)), e.get("credited"))
        # One coldkey per surviving hotkey: dedup already happened in the stored epoch.
        out.append(MinerEvidence(int(m["uid"]), hk, hk, reg, tiers, bool(m.get("serious_failure", False))))
    return out


def recompute(record: dict, prev_state: dict | None) -> dict:
    """Return {mode: V3Result} for every readiness mode. Pure."""
    miners = miners_from_record(record)
    demand = record["inputs"].get("demand", {})
    epoch = int(record["epoch_start_block"])
    kw = dict(readiness_split=float(record.get("readiness_split", READINESS_SPLIT)),
              burn_unearned=bool(record.get("burn_unearned", True)))
    return {mode: allocate_v3(miners, demand, prev_state, epoch, readiness_mode=mode, **kw)
            for mode in READINESS_MODES}


def summarize(record: dict, results: dict) -> dict:
    recorded_mode = record.get("readiness_mode", "slots")
    recorded = {int(u): float(v) for u, v in record.get("units_by_uid", {}).items()}
    uids = sorted(set(recorded).union(*(r.units_by_uid for r in results.values())))
    hotkeys = {}
    for r in results.values():
        hotkeys.update(r.hotkey_by_uid)
    rows = [{"uid": u, "hotkey": hotkeys.get(u, ""), "recorded": recorded.get(u, 0.0),
             **{m: results[m].units_by_uid.get(u, 0.0) for m in results}} for u in uids]
    mine = results[recorded_mode].units_by_uid
    drift = max((abs(mine.get(u, 0.0) - recorded.get(u, 0.0)) for u in uids), default=0.0)
    return {
        "epoch_start_block": int(record["epoch_start_block"]),
        "recorded_mode": recorded_mode,
        "recorded_burn_units": float(record.get("burn_units", 0.0)),
        "max_abs_diff_vs_record": drift,
        "rows": rows,
        "totals": {m: {"paid": math.fsum(r.units_by_uid.values()), "burn": r.burn_units,
                       "burn_by_reason": dict(sorted(r.burn_by_reason.items())),
                       "paid_miners": len(r.units_by_uid)} for m, r in results.items()},
    }


def render(summary: dict) -> str:
    modes = list(summary["totals"])
    lines = [f"epoch {summary['epoch_start_block']}  recorded mode: {summary['recorded_mode']}  "
             f"recompute vs record max |diff|: {summary['max_abs_diff_vs_record']:.3g}",
             f"{'uid':>5}  {'recorded':>9}  " + "  ".join(f"{m:>9}" for m in modes) + "  hotkey"]
    for r in summary["rows"]:
        lines.append(f"{r['uid']:>5}  {r['recorded']:>9.4f}  " + "  ".join(f"{r[m]:>9.4f}" for m in modes)
                     + f"  {r['hotkey']}")
    lines.append(f"{'paid':>5}  {TOTAL_UNITS - summary['recorded_burn_units']:>9.4f}  "
                 + "  ".join(f"{summary['totals'][m]['paid']:>9.4f}" for m in modes))
    lines.append(f"{'burn':>5}  {summary['recorded_burn_units']:>9.4f}  "
                 + "  ".join(f"{summary['totals'][m]['burn']:>9.4f}" for m in modes))
    for m in modes:
        reasons = ", ".join(f"{k}={v:.4f}" for k, v in summary["totals"][m]["burn_by_reason"].items() if v > 0)
        lines.append(f"{m} burn by reason: {reasons or 'none'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m v3_whatif", description=__doc__.splitlines()[0])
    ap.add_argument("record", help="stored epoch record (epochs/<epoch_start_block>.json)")
    ap.add_argument("--prev-record", help="record of the previous epoch (its state_after is the input state)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    record = load_record(args.record)
    prev_path = Path(args.prev_record) if args.prev_record else find_prev_record(args.record)
    prev_state = load_record(prev_path).get("state_after") if prev_path else None
    if prev_path is None:
        print("note: no previous record found; recomputing from an empty state", file=sys.stderr)
    summary = summarize(record, recompute(record, prev_state))
    summary["prev_record"] = str(prev_path) if prev_path else None
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
