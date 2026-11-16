"""Spec475 regression: LastUpdate advances on commit, not on CR reveal."""
from types import SimpleNamespace
from pathlib import Path
import json

import pytest

from tests.test_weight_loop_durable import Chain, adapter, submit
from tests.chain_v2_fake import MASTER
from weight_application_proof import find_application_proof, MAX_APPLICATION_SCAN_BLOCKS


class Runtime475Chain(Chain):
    def __init__(self):
        super().__init__()
        self.rows[353] = [(1, 65535)]
        self.updated[353] = 1100
        self.events = {}
        self.history = {}
        self.pending_reads = 0

    def apply(self, ext):
        super().apply(ext)
        index = 353 + ext["mechid"] * 4096
        if ext["commit_reveal"]:
            self.updated[index] = self.head  # Observed live spec475 behavior.
        else:
            self.event(index)

    def event(self, index=353):
        self.events.setdefault(self.head, []).append({
            "module_id": "SubtensorModule", "event_id": "WeightsSet", "attributes": [index, 0],
        })
        self.history[self.head, index] = list(self.rows.get(index, []))

    def reveal(self, index=353):
        self.head += 1
        self.commits.clear()
        self.rows[index] = [(1, 65535)]
        self.event(index)
        # Deliberately do not change LastUpdate during CR reveal.

    def query_map(self, **kwargs):
        self.pending_reads += 1
        return super().query_map(**kwargs)

    def query(self, *, module, storage_function, params, block_hash):
        block = int(block_hash, 16)
        if (module, storage_function) == ("System", "Events"):
            return SimpleNamespace(value=self.events.get(block, []))
        if storage_function == "Weights" and (block, params[0]) in self.history:
            return SimpleNamespace(value=self.history[block, params[0]])
        return super().query(module=module, storage_function=storage_function, params=params, block_hash=block_hash)


def test_same_vector_commit_advances_last_update_but_is_pending(tmp_path):
    chain = Runtime475Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, cr=True).status == "pending_reveal"
    assert chain.updated[353] == 1201
    assert chain.rows[353] == [(1, 65535)]
    assert chain.pending_reads >= 2
    assert adapter(chain, tmp_path).reconcile(0).status == "pending_reveal"
    assert len(chain.calls) == 1


def test_pruned_commit_with_identical_old_row_is_not_confirmed(tmp_path):
    chain = Runtime475Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, cr=True).status == "pending_reveal"
    chain.commits.clear()  # Could be failed/decryption-expired, not proof of reveal.
    chain.head += 10
    assert adapter(chain, tmp_path).reconcile(0).status == "waiting"
    assert len(chain.calls) == 1


def test_same_vector_reveal_event_proves_application_without_last_update_change(tmp_path):
    chain = Runtime475Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, cr=True).status == "pending_reveal"
    committed_update = chain.updated[353]
    chain.reveal()
    assert chain.updated[353] == committed_update
    assert adapter(chain, tmp_path).reconcile(0).status == "confirmed"


def test_even_application_event_does_not_release_known_pending_fence(tmp_path):
    chain = Runtime475Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, cr=True).status == "pending_reveal"
    chain.event()
    assert writer.reconcile(0).status == "pending_reveal"


def proof(chain, *, bound=1200, update=1201, head=None, mechid=0):
    return find_application_proof(
        chain, netuid=353, mechid=mechid, hotkey=MASTER.ss58_address, uid=0,
        expected_row=((1, 65535),), intent_block=bound, last_update=update,
        head=SimpleNamespace(block=head or chain.head, hash=hex(head or chain.head)),
    )


def test_predispatch_finalized_intent_event_is_not_new_application():
    chain = Runtime475Chain()
    chain.event()  # 1200 was already finalized when dispatch intent was recorded.
    chain.head = 1210
    assert proof(chain, update=1200)[0] is None


@pytest.mark.parametrize("attributes", [[353, 1], [4449, 0], [31, 0], [353, True], {"netuid": 353, "uid": 0}])
def test_wrong_or_unsupported_event_identity_is_not_proof(attributes):
    chain = Runtime475Chain()
    chain.head = 1202
    chain.event()
    chain.events[1202][0]["attributes"] = attributes
    assert proof(chain)[0] is None


def test_historical_hotkey_uid_binding_required():
    chain = Runtime475Chain()
    chain.head = 1202
    chain.event()
    query = chain.query
    def rebound(**kwargs):
        if kwargs["storage_function"] == "Uids":
            return SimpleNamespace(value=9)
        return query(**kwargs)
    chain.query = rebound
    assert proof(chain)[0] is None


def test_historical_exact_row_required_even_when_current_row_matches():
    chain = Runtime475Chain()
    chain.head = 1202
    chain.event()
    chain.history[1202, 353] = [(2, 65535)]
    assert proof(chain)[0] is None


def test_encoded_mechanism_index_uses_base_subnet_uid_lookup():
    chain = Runtime475Chain()
    chain.head = 1202
    chain.rows[4449] = [(1, 65535)]
    chain.event(4449)
    query = chain.query
    def check(**kwargs):
        if kwargs["storage_function"] == "Uids":
            assert kwargs["params"] == [353, MASTER.ss58_address]
        return query(**kwargs)
    chain.query = check
    assert proof(chain, mechid=1)[0].block == 1202


@pytest.mark.parametrize("failure", ["raise", "missing", "malformed"])
def test_unavailable_or_malformed_events_never_confirm(failure):
    chain = Runtime475Chain()
    chain.head = 1202
    query = chain.query
    def unavailable(**kwargs):
        if kwargs["storage_function"] == "Events":
            if failure == "raise":
                raise OSError("pruned")
            return SimpleNamespace(value=None if failure == "missing" else [None])
        return query(**kwargs)
    chain.query = unavailable
    assert proof(chain)[0] is None


def test_missing_historical_hash_never_falls_back_to_live():
    chain = Runtime475Chain()
    chain.head = 1202
    chain.get_block_hash = lambda block: None
    assert proof(chain)[0] is None


def test_application_scan_is_bounded_and_old_proof_not_invented():
    chain = Runtime475Chain()
    chain.head = 1201
    chain.event()
    chain.head += MAX_APPLICATION_SCAN_BLOCKS
    result, reason = proof(chain)
    assert result is None
    assert "bound reached" in reason


def test_unproven_pruned_commit_can_expire_but_never_confirms(tmp_path):
    chain = Runtime475Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, cr=True).status == "pending_reveal"
    chain.commits.clear()
    chain.head += 1000
    outcome = writer.reconcile(0)
    assert not outcome.success and outcome.status == "waiting"
    assert "expired safely" in outcome.message
    assert writer.reconcile(0) is None


def test_pending_scan_failure_cannot_expire_or_confirm(tmp_path):
    chain = Runtime475Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, cr=True).status == "pending_reveal"
    chain.head += 1000
    def unavailable(**kwargs):
        raise OSError("pending map unreadable")
    chain.query_map = unavailable
    with pytest.raises(OSError, match="pending map unreadable"):
        writer.reconcile(0)
    assert len(chain.calls) == 1


def test_pending_scan_over_safety_bound_fails_closed(tmp_path):
    chain = Runtime475Chain()
    writer = adapter(chain, tmp_path)
    assert submit(writer, cr=True).status == "pending_reveal"
    chain.commits = {(353, n): [] for n in range(65)}
    chain.head += 1000
    with pytest.raises(Exception, match="scan exceeds safety bound"):
        writer.reconcile(0)


def test_live_capture_disproves_last_update_as_reveal_marker():
    capture = json.loads((Path(__file__).parent / "fixtures/finney_475_commit_reveal.json").read_text())
    before, after = capture["commit"]["before"], capture["commit"]["after"]
    assert before["weights"] == after["weights"]
    assert after["last_update"] > before["last_update"]
    assert len(after["matching_pending"]) > len(before["matching_pending"])
    assert all(e["event_id"] != "WeightsSet" for e in capture["commit"]["events"])
    reveal = capture["reveal"]
    assert any(e["event_id"] == "WeightsSet" for e in reveal["events"])
    assert reveal["before_last_update"] == reveal["after_last_update"]
    assert reveal["after_last_update"] == {"9": 9261054, "10": 9260859}


def test_legacy_reconcile_uses_native_keys_and_fences_preintent_pending():
    from bundle_chain import reconcile_weights, WeightStatus
    from bundle_schedule import finalized_head
    from tests.chain_v2_fake import FakeChain, FakeSubtensor, SLAVE, NETUID
    chain = FakeChain(head=2000)
    # Native epoch 22665 is unrelated to the application's 2000//360 sequence.
    chain.timelocked[22665] = [(SLAVE.ss58_address, 1000, "00", 1)]
    sub = FakeSubtensor(chain)
    result = reconcile_weights(
        sub, SLAVE.ss58_address, NETUID, epoch_start_block=900,
        subnet_epoch_index=2, expected_row=((1, 65535),), intent_block=1200,
        head=finalized_head(sub), commit_epochs=(2, 3),
    )
    assert result is WeightStatus.PENDING


def test_missing_legacy_artifact_does_not_expire_older_native_pending():
    from bundle_consume import _reconcile_weights_op
    from tests.chain_v2_fake import FakeChain, FakeSubtensor, SLAVE, NETUID
    chain = FakeChain(head=2000)
    chain.timelocked[22665] = [(SLAVE.ss58_address, 1000, "00", 1)]
    observations = []
    journal = SimpleNamespace(observe_pending=lambda key: observations.append("pending"))
    record = SimpleNamespace(
        key=SimpleNamespace(subnet_epoch_index=2, epoch_start_block=900),
        cid="fixture", intent_block=1200, expiry_deadline=1500,
    )
    outcome = _reconcile_weights_op(
        FakeSubtensor(chain), SLAVE.ss58_address, SimpleNamespace(netuid=NETUID),
        SimpleNamespace(load=lambda *args: None), journal, record,
    )
    assert outcome.status == "pending"
    assert observations == ["pending"]


def test_direct_same_vector_write_also_needs_application_event(tmp_path):
    chain = Runtime475Chain()
    assert submit(adapter(chain, tmp_path), cr=False).status == "confirmed"
