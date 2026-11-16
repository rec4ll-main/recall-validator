"""Fixture tests for the one-hotkey-per-coldkey incentive dedup.

Run from the repository root:
    uv run --frozen pytest tests/test_coldkey_dedup.py
"""

import sys

from main import dedup_recommended_uids


class StubMetagraph:
    def __init__(self, coldkeys, reg_blocks):
        self.coldkeys = coldkeys
        self.block_at_registration = reg_blocks


def test_coldkey_three_hotkeys_latest_survives():
    coldkeys = ["ckA", "ckA", "ckA", "ckB"]
    reg = [100, 300, 200, 50]
    resolved = [(0, 0.3), (1, 0.3), (2, 0.3), (3, 0.1)]
    out = dedup_recommended_uids(resolved, coldkeys, reg)
    uids = sorted(uid for uid, _ in out)
    assert uids == [1, 3], f"expected latest-registered uid 1 + single coldkey uid 3, got {uids}"
    assert dict(out)[1] == 0.3
    assert dict(out)[3] == 0.1


def test_all_single_hotkey_unchanged():
    coldkeys = ["ckA", "ckB", "ckC"]
    reg = [100, 200, 300]
    resolved = [(0, 0.4), (1, 0.35), (2, 0.25)]
    out = dedup_recommended_uids(resolved, coldkeys, reg)
    assert sorted(uid for uid, _ in out) == [0, 1, 2]
    for uid, weight in out:
        assert weight == dict(resolved)[uid]


def test_survivors_renormalize_to_one():
    from main import renormalize_resolved

    coldkeys = ["ckA", "ckA", "ckB"]
    reg = [100, 200, 50]
    resolved = [(0, 0.6), (1, 0.3), (2, 0.1)]
    survivors = dedup_recommended_uids(resolved, coldkeys, reg)
    assert sorted(uid for uid, _ in survivors) == [1, 2]
    renormalized = renormalize_resolved(survivors)
    total = sum(weight for _, weight in renormalized)
    assert abs(total - 1.0) < 1e-9, f"expected renormalized sum 1.0, got {total}"
    weights = dict(renormalized)
    assert abs(weights[1] - 0.75) < 1e-9, f"expected uid 1 weight 0.75, got {weights[1]}"
    assert abs(weights[2] - 0.25) < 1e-9, f"expected uid 2 weight 0.25, got {weights[2]}"


def test_reg_block_tie_lowest_uid_wins():
    coldkeys = ["ckA", "ckA"]
    reg = [200, 200]
    resolved = [(0, 0.4), (1, 0.6)]
    out = dedup_recommended_uids(resolved, coldkeys, reg)
    uids = [uid for uid, _ in out]
    assert uids == [0], f"expected lowest-uid survivor on tie, got {uids}"


def test_empty_dedup_no_crash():
    coldkeys = ["ckA", "ckA"]
    reg = [100, 200]
    assert dedup_recommended_uids([], coldkeys, reg) == []
    assert dedup_recommended_uids(None, coldkeys, reg) is None
    assert dedup_recommended_uids([], None, None) == []


def test_missing_metagraph_attrs_defensive():
    from main import _metagraph_registration_blocks

    class NoAttrs:
        pass

    ck, rb = _metagraph_registration_blocks(NoAttrs())
    assert ck is None and rb is None


if __name__ == "__main__":
    tests = [
        test_coldkey_three_hotkeys_latest_survives,
        test_all_single_hotkey_unchanged,
        test_survivors_renormalize_to_one,
        test_reg_block_tie_lowest_uid_wins,
        test_empty_dedup_no_crash,
        test_missing_metagraph_attrs_defensive,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print("PASS %s" % test.__name__)
        except Exception as exc:
            failures += 1
            import traceback

            print("FAIL %s: %s" % (test.__name__, exc))
            traceback.print_exc()
    print("dedup fixture tests: %d passed, %d failed" % (len(tests) - failures, failures))
    sys.exit(1 if failures else 0)
