"""Offline regressions for the real journal and dispatch-probe weight adapter."""

from types import SimpleNamespace

import pytest

from tests.verified_storage_fake import VerifiedStorageFake

from bundle_journal_types import ArtifactBinding, OperationKey, OperationKind
from bundle_runtime import _journal_for
from tests.chain_v2_fake import GENESIS, MASTER
from weight_loop import sdk_submitter
from weight_loop_dispatch import DurableWeightSubmitter


class Chain(VerifiedStorageFake):
    def __init__(self):
        self.head = 1200
        self.rows = {}
        self.updated = {}
        self.commits = {}
        self.calls = []
        self.mode = "ok"
        self.substrate = self
        self.registered = True
        self.events = {}

    def get_block_hash(self, block):
        return GENESIS if block == 0 else hex(block)

    def get_chain_finalised_head(self):
        return hex(self.head)

    def get_block_number(self, block_hash):
        return int(block_hash, 16)

    def query(self, *, module, storage_function, params, block_hash):
        if (module, storage_function) == ("System", "Events"):
            value = self.events.get(int(block_hash, 16), [])
        elif storage_function == "Uids":
            value = 0 if self.registered else None
        elif storage_function == "Keys":
            value = MASTER.ss58_address if self.registered and params[1] == 0 else None
        elif storage_function == "MechanismCountCurrent":
            value = 2
        elif storage_function == "LastUpdate":
            value = [self.updated.get(params[0], 0)]
        elif storage_function == "Weights":
            value = self.rows.get(params[0], [])
        elif storage_function == "TimelockedWeightCommits":
            value = self.commits.get(tuple(params), [])
        else:
            raise AssertionError(storage_function)
        return SimpleNamespace(value=value)

    def query_map(self, *, module, storage_function, params, block_hash):
        assert storage_function == "TimelockedWeightCommits"
        return [(epoch, entries) for (index, epoch), entries in self.commits.items()
                if index == params[0]]

    def apply(self, ext):
        self.head += 1
        index = 353 + ext["mechid"] * 4096
        if ext["commit_reveal"]:
            # Native epoch deliberately differs from head//tempo.
            self.commits[index, 7] = [(MASTER.ss58_address, self.head, "0xab", 1)]
            self.updated[index] = self.head
        else:
            self.rows[index] = [(1, 65535)]
            self.updated[index] = self.head
            self.events[self.head] = [{"module_id": "SubtensorModule", "event_id": "WeightsSet", "attributes": [index, 0]}]

    def submit_extrinsic(self, ext, **kwargs):
        self.calls.append(ext)
        if self.mode == "timeout":
            raise TimeoutError("subscription lost after broadcast")
        if self.mode == "crash":
            raise KeyboardInterrupt()
        if self.mode == "failed":
            return SimpleNamespace(is_success=False, finalized=True,
                                   error_message="IncorrectWeightVersionKey")
        self.apply(ext)
        return SimpleNamespace(is_success=True, finalized=True, error_message=None)

    def send(self, **kwargs):
        if self.mode == "pre-send":
            raise OSError("read failed before broadcast")
        receipt = self.substrate.submit_extrinsic(kwargs)
        return SimpleNamespace(success=receipt.is_success,
                               message=receipt.error_message or "ok")


def adapter(chain, tmp_path):
    return DurableWeightSubmitter(chain, MASTER.ss58_address, 353,
                                  str(tmp_path), GENESIS, chain.send)


def submit(writer, mechid=0, cr=False):
    return writer(mechid=mechid, uids=[1], weights=[1.0], version_key=0,
                  commit_reveal=cr)


def test_finalized_exact_row_is_confirmed(tmp_path):
    chain = Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer).status == "confirmed"
    assert writer.reconcile(0) is None
    assert len(chain.calls) == 1


@pytest.mark.parametrize("mode", ["timeout", "crash"])
def test_possibly_broadcast_attempt_survives_restart(tmp_path, mode):
    chain = Chain()
    chain.mode = mode
    writer = adapter(chain, tmp_path)
    if mode == "crash":
        with pytest.raises(KeyboardInterrupt):
            submit(writer)
    else:
        assert submit(writer).status == "waiting"
    restarted = adapter(chain, tmp_path)
    chain.head += 4
    assert submit(restarted).status == "waiting"
    assert len(chain.calls) == 1
    chain.apply(chain.calls[0])
    assert restarted.reconcile(0).status == "confirmed"
    assert restarted.reconcile(0) is None


def test_unknown_requires_exact_row_not_just_last_update(tmp_path):
    chain = Chain()
    chain.mode = "timeout"
    writer = adapter(chain, tmp_path)
    submit(writer)
    chain.head += 2
    chain.updated[353] = chain.head
    chain.rows[353] = [(2, 65535)]
    assert writer.reconcile(0).status == "waiting"
    assert len(chain.calls) == 1


def test_timeout_expires_without_blind_retry(tmp_path):
    chain = Chain()
    chain.mode = "timeout"
    writer = adapter(chain, tmp_path)
    submit(writer)
    chain.head += 255
    assert submit(writer).status == "waiting"
    assert len(chain.calls) == 1
    chain.head += 1
    assert writer.reconcile(0).message == "prior attempt expired safely"
    assert len(chain.calls) == 1
    chain.mode = "ok"
    assert submit(writer).status == "confirmed"
    assert len(chain.calls) == 2


def test_pending_commit_stays_fenced_after_mortality_and_restart(tmp_path):
    chain = Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, cr=True).status == "pending_reveal"
    chain.head += 1000
    restarted = adapter(chain, tmp_path)
    assert submit(restarted, cr=True).status == "pending_reveal"
    assert len(chain.calls) == 1
    chain.commits.clear()
    chain.rows[353] = [(1, 65535)]
    chain.events[chain.head] = [{"module_id": "SubtensorModule", "event_id": "WeightsSet", "attributes": [353, 0]}]
    assert restarted.reconcile(0).status == "confirmed"


def test_pending_mechanism_does_not_block_other_mechanism(tmp_path):
    chain = Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, mechid=1, cr=True).status == "pending_reveal"
    assert submit(writer, mechid=0).status == "confirmed"
    assert writer.reconcile(1).status == "pending_reveal"
    assert len(chain.calls) == 2


def test_unjournaled_pending_commit_fences_first_dispatch(tmp_path):
    chain = Chain()
    chain.commits[353, 7] = [(MASTER.ss58_address, 1000, "0xab", 1)]
    assert submit(adapter(chain, tmp_path)).status == "pending_reveal"
    assert not chain.calls


def test_pending_commit_read_failure_never_broadcasts(tmp_path):
    chain = Chain()
    def unavailable(**kwargs):
        raise OSError("pending queue unavailable")
    chain.query_map = unavailable
    with pytest.raises(OSError, match="pending queue unavailable"):
        submit(adapter(chain, tmp_path))
    assert not chain.calls


def test_unbroadcast_failure_is_retryable(tmp_path):
    chain = Chain()
    chain.mode = "pre-send"
    writer = adapter(chain, tmp_path)
    assert not submit(writer).success
    assert not chain.calls
    chain.mode = "ok"
    assert submit(writer).status == "confirmed"
    assert len(chain.calls) == 1


def test_finalized_failure_releases_fence_but_not_same_block(tmp_path):
    chain = Chain()
    chain.mode = "failed"
    writer = adapter(chain, tmp_path)
    assert not submit(writer).success
    assert writer.reconcile(0) is None
    assert submit(writer).status == "waiting"
    chain.head += 1
    chain.mode = "ok"
    assert submit(writer).status == "confirmed"
    assert len(chain.calls) == 2


def test_legacy_unknown_fences_new_loop(tmp_path):
    chain = Chain()
    journal = _journal_for(str(tmp_path))
    key = OperationKey(GENESIS, 353, MASTER.ss58_address, 1000, 2, OperationKind.WEIGHTS_SUBMIT)
    binding = ArtifactBinding("0x" + "ab" * 32, "legacy-row")
    journal.prepare(key, binding)
    journal.begin(key, intent_block=1000, binding=binding)
    journal.close()
    assert submit(adapter(chain, tmp_path)).status == "waiting"
    assert not chain.calls


def test_restore_and_missing_journal_fences_are_preserved(tmp_path):
    chain = Chain()
    writer = adapter(chain, tmp_path)
    journal = _journal_for(str(tmp_path))
    journal.mark_restored()
    journal.close()
    assert submit(writer).status == "waiting"
    assert not chain.calls
    (tmp_path / "journal.sqlite3").unlink()
    with pytest.raises(Exception, match="journal lost"):
        submit(writer)
    assert not chain.calls


def test_wrong_genesis_never_submits(tmp_path):
    chain = Chain()
    writer = adapter(chain, tmp_path)
    writer.genesis_hash = "0x" + "cd" * 32
    with pytest.raises(Exception, match="genesis hash mismatch"):
        submit(writer)
    assert not chain.calls


@pytest.mark.parametrize("cr", [False, True])
def test_sdk_adapter_uses_mortality_finalization_and_correct_route(tmp_path, monkeypatch, cr):
    from bittensor.core.extrinsics import weights
    from bundle_dispatch import MORTALITY_PERIOD
    chain = Chain()
    called = []
    def sdk_call(**kwargs):
        called.append(kwargs)
        return chain.send(mechid=kwargs["mechid"], commit_reveal=cr)
    monkeypatch.setattr(weights, "commit_timelocked_weights_extrinsic" if cr
                        else "set_weights_extrinsic", sdk_call)
    wallet = SimpleNamespace(hotkey=MASTER)
    writer = sdk_submitter(chain, wallet, 353, state_dir=str(tmp_path), genesis_hash=GENESIS)
    assert submit(writer, cr=cr).success
    assert len(called) == 1
    assert called[0]["period"] == MORTALITY_PERIOD
    assert called[0]["wait_for_finalization"] is True
    assert called[0]["version_key"] == 0
