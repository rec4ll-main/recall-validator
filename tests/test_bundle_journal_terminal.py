"""Journal terminal failure states and the v2 -> v3 schema migration."""

from __future__ import annotations

import sqlite3

import pytest

from bundle_journal import BundleJournal
from bundle_journal_store import SCHEMA_VERSION
from bundle_journal_types import (
    ArtifactBinding,
    JournalConflictError,
    JournalSchemaError,
    JournalState,
    JournalStateError,
    ObservationRef,
    OperationKey,
    OperationKind,
)
from chain_v2_fake import GENESIS, NETUID, SLAVE

BINDING = ArtifactBinding(digest="0x" + "11" * 32, cid="bafkreitestcid")
OTHER = ArtifactBinding(digest="0x" + "22" * 32, cid="bafkreiothercid")


def _key(epoch: int, kind=OperationKind.WEIGHTS_SUBMIT) -> OperationKey:
    return OperationKey(
        genesis_hash=GENESIS, netuid=NETUID, wallet_hotkey=SLAVE.ss58_address,
        epoch_start_block=epoch * 360, subnet_epoch_index=epoch, operation_kind=kind,
    )


def _ref(text: str) -> ObservationRef:
    return ObservationRef(value=text)


@pytest.fixture()
def journal(tmp_path):
    j = BundleJournal.create_new(tmp_path / "journal.sqlite3")
    yield j
    j.close()


def _intent(j, epoch, block=100):
    j.prepare(_key(epoch), BINDING)
    assert j.begin(_key(epoch), intent_block=block) is not None


@pytest.mark.parametrize("mark", ["not_dispatched", "failed", "expired"])
def test_terminal_failure_states_release_the_cross_epoch_gate(journal, mark):
    _intent(journal, 10)
    with pytest.raises(JournalConflictError, match="cross-epoch"):
        journal.prepare(_key(11), BINDING)

    if mark == "not_dispatched":
        journal.mark_not_dispatched(_key(10), _ref("not_dispatched: rate limit"))
    elif mark == "failed":
        journal.mark_failed(_key(10), _ref("failed: ExtrinsicFailed"))
    else:
        journal.stamp_expiry_deadline(_key(10), 500)
        journal.mark_expired(_key(10), _ref("expired"), finalized_block=500)

    assert journal.unresolved(SLAVE.ss58_address, OperationKind.WEIGHTS_SUBMIT) == []
    journal.prepare(_key(11), BINDING)
    assert journal.begin(_key(11), intent_block=600) is not None


def test_only_not_dispatched_is_rearmable(journal):
    _intent(journal, 10)
    journal.mark_not_dispatched(_key(10), _ref("not_dispatched: x"))
    auth = journal.begin(_key(10), intent_block=200, binding=BINDING)
    assert auth is not None
    rec = journal.record(_key(10))
    assert (rec.state, rec.intent_block, rec.outcome_ref, rec.expiry_deadline) == (
        JournalState.INTENT, 200, None, None,
    )
    journal.mark_failed(_key(10), _ref("failed: x"))
    assert journal.begin(_key(10), intent_block=300) is None

    _intent(journal, 11)
    journal.stamp_expiry_deadline(_key(11), 400)
    journal.mark_expired(_key(11), _ref("expired"), finalized_block=400)
    assert journal.begin(_key(11), intent_block=500) is None


def test_rearm_rejects_a_different_binding(journal):
    _intent(journal, 10)
    journal.mark_not_dispatched(_key(10), _ref("not_dispatched: x"))
    with pytest.raises(JournalConflictError, match="conflicting artifact binding"):
        journal.begin(_key(10), intent_block=200, binding=OTHER)
    assert journal.state(_key(10)) is JournalState.NOT_DISPATCHED


def test_not_dispatched_only_from_live_intent(journal):
    _intent(journal, 10)
    journal.mark_unknown(_key(10))  # e.g. after a restart
    with pytest.raises(JournalStateError):
        journal.mark_not_dispatched(_key(10), _ref("not_dispatched: x"))


def test_expired_requires_a_finalized_deadline(journal):
    _intent(journal, 10)
    journal.mark_unknown(_key(10))
    with pytest.raises(JournalStateError, match="deadline"):
        journal.mark_expired(_key(10), _ref("expired"), finalized_block=10_000)
    assert journal.stamp_expiry_deadline(_key(10), 700) == 700
    assert journal.stamp_expiry_deadline(_key(10), 900) == 700  # first stamp wins
    assert journal.stamp_expiry_deadline(_key(10), 100) == 700
    with pytest.raises(JournalStateError, match="deadline"):
        journal.mark_expired(_key(10), _ref("expired"), finalized_block=699)
    journal.mark_expired(_key(10), _ref("expired"), finalized_block=700)
    journal.mark_expired(_key(10), _ref("expired"), finalized_block=800)  # idempotent
    with pytest.raises(JournalConflictError):
        journal.mark_expired(_key(10), _ref("other"), finalized_block=800)


@pytest.mark.parametrize("op", ["observe", "observe_pending", "mark_unknown", "stamp"])
def test_failed_and_expired_are_terminal(journal, op):
    _intent(journal, 10)
    journal.mark_failed(_key(10), _ref("failed: x"))
    with pytest.raises(JournalStateError):
        if op == "observe":
            journal.observe(_key(10), _ref("applied:x"))
        elif op == "observe_pending":
            journal.observe_pending(_key(10))
        elif op == "mark_unknown":
            journal.mark_unknown(_key(10))
        else:
            journal.stamp_expiry_deadline(_key(10), 1)


def test_pending_and_unknown_can_fail_but_prepared_cannot(journal):
    journal.prepare(_key(10), BINDING)
    with pytest.raises(JournalStateError):
        journal.mark_failed(_key(10), _ref("failed: x"))
    journal.begin(_key(10), intent_block=1)
    journal.observe_pending(_key(10))
    journal.mark_failed(_key(10), _ref("failed: x"))
    assert journal.state(_key(10)) is JournalState.FAILED


def test_restore_fence_clears_with_only_terminal_rows(tmp_path):
    j = BundleJournal.create_new(tmp_path / "journal.sqlite3")
    _intent(j, 10)
    j.mark_failed(_key(10), _ref("failed: x"))
    j.mark_restored(note="test")
    j.clear_restore_fence()
    assert not j.restore_fenced
    j.close()


def test_inconsistent_outcome_reference_fails_closed(tmp_path):
    path = tmp_path / "journal.sqlite3"
    j = BundleJournal.create_new(path)
    _intent(j, 10)
    j.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE operations SET state = 'failed'")
    conn.commit()
    conn.close()
    with pytest.raises(JournalSchemaError, match="inconsistent outcome reference"):
        BundleJournal.open_existing(path)


# --------------------------------------------------------------------------- #
# v2 -> v3 migration.
# --------------------------------------------------------------------------- #

_V2_SQL = """
CREATE TABLE schema_meta (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE operations (
    genesis_hash TEXT NOT NULL,
    netuid INTEGER NOT NULL,
    wallet_hotkey TEXT NOT NULL,
    epoch_start_block TEXT NOT NULL,
    subnet_epoch_index TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    artifact_cid TEXT NOT NULL,
    state TEXT NOT NULL,
    observation_ref TEXT,
    intent_block TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (genesis_hash, netuid, wallet_hotkey, epoch_start_block,
                 subnet_epoch_index, operation_kind)
);
INSERT INTO schema_meta VALUES ('schema_version', '2');
"""


def _v2_journal(path, *, extra_sql: str = ""):
    conn = sqlite3.connect(path)
    conn.executescript(_V2_SQL + extra_sql)
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (GENESIS, NETUID, SLAVE.ss58_address, "3600", "10", "weights_submit",
         BINDING.digest, BINDING.cid, "unknown", None, "3610", "t", "t"),
    )
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (GENESIS, NETUID, SLAVE.ss58_address, "3240", "9", "weights_submit",
         BINDING.digest, BINDING.cid, "observed", "applied:x", "3250", "t", "t"),
    )
    conn.commit()
    conn.close()


def test_v2_journal_is_migrated_in_place_and_keeps_its_rows(tmp_path):
    path = tmp_path / "journal.sqlite3"
    _v2_journal(path)

    j = BundleJournal.open_existing(path)
    rec = j.record(_key(10))
    assert rec.state is JournalState.UNKNOWN
    assert (rec.intent_block, rec.expiry_deadline, rec.outcome_ref) == (3610, None, None)
    assert j.state(_key(9)) is JournalState.OBSERVED
    # The legacy UNKNOWN can now be fenced by deadline and expired.
    j.stamp_expiry_deadline(_key(10), 4000)
    j.mark_expired(_key(10), _ref("expired"), finalized_block=4000)
    j.close()

    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone() == (SCHEMA_VERSION,)
    conn.close()
    BundleJournal.open_existing(path).close()  # v3 reopens cleanly


def test_drifted_v2_journal_is_not_migrated(tmp_path):
    path = tmp_path / "journal.sqlite3"
    _v2_journal(path, extra_sql="CREATE INDEX extra_idx ON operations(state);")
    with pytest.raises(JournalSchemaError, match="unexpected schema object"):
        BundleJournal.open_existing(path)
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone() == ("2",)
    conn.close()


def test_unknown_schema_version_still_fails_closed(tmp_path):
    path = tmp_path / "journal.sqlite3"
    _v2_journal(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = '1'")
    conn.commit()
    conn.close()
    with pytest.raises(JournalSchemaError, match="schema version mismatch"):
        BundleJournal.open_existing(path)
