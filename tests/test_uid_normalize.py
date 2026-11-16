
"""UID/weight pair normalization for signed envelopes."""

from __future__ import annotations

from allocation import normalize_uid_weight_pairs


def test_normalize_already_sorted_identity():
    uids, weights, hotkeys = normalize_uid_weight_pairs(
        [1, 5, 12], [0.2, 0.3, 0.5], ["a", "b", "c"]
    )
    assert uids == [1, 5, 12]
    assert weights == [0.2, 0.3, 0.5]
    assert hotkeys == ["a", "b", "c"]


def test_normalize_sums_duplicate_uids():
    uids, weights, hotkeys = normalize_uid_weight_pairs(
        [3, 1, 3], [0.1, 0.2, 0.3], ["x", "y", "z"]
    )
    assert uids == [1, 3]
    assert weights == [0.2, 0.4]
    assert hotkeys == ["y", "x"]
