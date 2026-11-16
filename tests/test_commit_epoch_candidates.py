"""Unit tests for cross-epoch timelocked commit epoch scanning."""

from __future__ import annotations

import pytest

from bundle_chain import COMMIT_EPOCH_SCAN_MAX, ChainError, commit_epoch_candidates
from bundle_dispatch import EXPIRY_MARGIN_BLOCKS


def test_commit_epoch_candidates_covers_mortality_span_and_current():
    # tempo 360, margin 256 -> span 1; journal 10 -> 10..11
    assert commit_epoch_candidates(journal_epoch=10, tempo=360) == (10, 11)
    # current ahead of mortality span
    assert commit_epoch_candidates(
        journal_epoch=10, tempo=360, current_epoch=14
    ) == (10, 11, 12, 13, 14)


def test_commit_epoch_candidates_small_tempo_spans_multiple_epochs():
    span = (EXPIRY_MARGIN_BLOCKS + 100 - 1) // 100
    assert span == 3
    assert commit_epoch_candidates(journal_epoch=5, tempo=100) == (5, 6, 7, 8)


def test_commit_epoch_candidates_without_tempo_covers_next_epoch():
    assert commit_epoch_candidates(journal_epoch=7) == (7, 8)


def test_commit_epoch_candidates_caps_scan():
    epochs = commit_epoch_candidates(journal_epoch=0, tempo=1, current_epoch=100)
    assert len(epochs) == COMMIT_EPOCH_SCAN_MAX + 1
    assert epochs[0] == 0
    assert epochs[-1] == COMMIT_EPOCH_SCAN_MAX


def test_commit_epoch_candidates_rejects_bad_inputs():
    with pytest.raises(ChainError):
        commit_epoch_candidates(journal_epoch=-1, tempo=360)
    with pytest.raises(ChainError):
        commit_epoch_candidates(journal_epoch=1, tempo=0)
