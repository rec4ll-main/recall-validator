"""Regression: coldkey dedupe must preserve burn remainder (validator#1)."""
from __future__ import annotations

import ast
from pathlib import Path


def dedupe_keep_latest(resolved, coldkeys, reg_blocks):
    best = {}
    for uid, weight in resolved:
        ck = coldkeys[uid]
        reg = reg_blocks[uid]
        cur = best.get(ck)
        if cur is None or reg > cur[2] or (reg == cur[2] and uid < cur[0]):
            best[ck] = (uid, weight, reg)
    return [(uid, weight) for uid, weight, _ in sorted(best.values(), key=lambda x: x[0])]


def allocate_with_burn(resolved_after_dedupe, burn_uid=99):
    uids = [uid for uid, _ in resolved_after_dedupe]
    total_raw = sum(w for _, w in resolved_after_dedupe)
    if total_raw < 1.0 and burn_uid not in uids:
        return list(zip(uids + [burn_uid], [w for _, w in resolved_after_dedupe] + [1.0 - total_raw]))
    if total_raw <= 0:
        return [(burn_uid, 1.0)]
    return [(uid, w / total_raw) for uid, w in resolved_after_dedupe]


def test_three_miners_two_share_coldkey_preserves_burn():
    coldkeys = ["ckA", "ckA", "ckB"]
    reg_blocks = [10, 20, 15]
    resolved = [(0, 0.10), (1, 0.10), (2, 0.10)]
    deduped = dedupe_keep_latest(resolved, coldkeys, reg_blocks)
    assert {uid for uid, _ in deduped} == {1, 2}
    assert all(abs(w - 0.10) < 1e-9 for _, w in deduped)
    final = allocate_with_burn(deduped, burn_uid=99)
    by_uid = {uid: w for uid, w in final}
    assert abs(by_uid[1] - 0.10) < 1e-9
    assert abs(by_uid[2] - 0.10) < 1e-9
    assert abs(by_uid[99] - 0.80) < 1e-9
    assert abs(sum(by_uid.values()) - 1.0) < 1e-9


def test_old_renormalize_behavior_would_be_wrong():
    survivors = [(1, 0.10), (2, 0.10)]
    total = sum(w for _, w in survivors)
    wrong = [(uid, w / total) for uid, w in survivors]
    assert abs(wrong[0][1] - 0.5) < 1e-9
    right = allocate_with_burn(survivors, 99)
    assert abs(dict(right)[99] - 0.80) < 1e-9


def test_main_source_uses_burn_remainder_helper():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "weights_with_burn_remainder" in names
    assert "renormalize_resolved(deduped)" not in src
    assert "weights_with_burn_remainder(resolved" in src


if __name__ == "__main__":
    test_three_miners_two_share_coldkey_preserves_burn()
    test_old_renormalize_behavior_would_be_wrong()
    test_main_source_uses_burn_remainder_helper()
    print("ok")
