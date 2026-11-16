"""Commitment quota charges match the runtime's per-commitment 100-byte floor."""

import pytest
from bittensor.core import settings

import bundle_commit_guard
import bundle_publish
from bundle_ipfs import compute_raw_cid
from chain_v2_fake import MASTER, NETUID, FakeChain, FakeSubtensor, FakeWallet
from test_chain_v2_pointer_lifecycle import E1, Master

CID = compute_raw_cid(b"commitment quota boundary")


@pytest.fixture(autouse=True)
def plain_commitments(monkeypatch):
    # A plain rejected receipt is definitive; MEV uncertainty has separate coverage.
    monkeypatch.setattr(settings, "DEFAULT_MEV_PROTECTION", False)


@pytest.mark.parametrize("payload, cost", [(CID, 100), ("x" * 101, 101), ("é" * 51, 102)])
@pytest.mark.parametrize("remaining_delta", [-1, 0])
def test_fake_chain_enforces_total_commitment_charge(payload, cost, remaining_delta):
    chain = FakeChain(head=E1 + 1, frozen_epoch_block=E1)
    index = chain.chain_epoch_index(chain.head)
    initial_used = chain.max_space - cost - remaining_delta
    chain.used_space[MASTER.ss58_address] = (index, initial_used)
    sub = FakeSubtensor(chain)

    result = sub.set_commitment(FakeWallet(MASTER), NETUID, payload)

    assert result.success == (remaining_delta == 0)
    assert len(chain.submissions) == 1
    if result.success:
        assert chain.used_space[MASTER.ss58_address] == (index, initial_used + cost)
        assert chain.pointer_at(MASTER.ss58_address, chain.head)[1] == payload
    else:
        assert "SpaceLimitExceeded" in result.message
        assert chain.used_space[MASTER.ss58_address] == (index, initial_used)
        assert chain.pointer_at(MASTER.ss58_address, chain.head) is None


def test_frozen_epoch_fills_3100_bytes_after_31_raw_cids():
    chain = FakeChain(head=E1 + 1, frozen_epoch_block=E1)
    sub = FakeSubtensor(chain)
    wallet = FakeWallet(MASTER)
    for n in range(31):
        cid = compute_raw_cid(f"commitment {n}".encode())
        assert len(cid.encode("utf-8")) == 59
        assert sub.set_commitment(wallet, NETUID, cid).success
    last_pointer = chain.pointer_at(MASTER.ss58_address, chain.head)

    result = sub.set_commitment(wallet, NETUID, CID)

    assert not result.success and "SpaceLimitExceeded" in result.message
    assert chain.used_space[MASTER.ss58_address] == (E1 // chain.tempo, 3100)
    assert len(chain.commitments[MASTER.ss58_address]) == 31
    assert chain.pointer_at(MASTER.ss58_address, chain.head) == last_pointer


@pytest.mark.parametrize("remaining", [99, 100])
def test_master_checks_charge_before_ipfs_or_dispatch(tmp_path, monkeypatch, remaining):
    master = Master(tmp_path, monkeypatch)
    cid = master.prepare_epoch(E1)
    index = master.chain.chain_epoch_index(master.chain.head)
    initial_used = master.chain.max_space - remaining
    master.chain.used_space[MASTER.ss58_address] = (index, initial_used)
    ipfs_calls = []

    def publish(url, data, **kwargs):
        ipfs_calls.append("publish")
        return compute_raw_cid(data)

    monkeypatch.setattr(bundle_publish, "publish_block", publish)
    monkeypatch.setattr(bundle_publish, "pin_block", lambda *a, **k: ipfs_calls.append("pin"))
    monkeypatch.setattr(
        bundle_publish, "read_back_block", lambda *a, **k: ipfs_calls.append("read_back")
    )

    result = master.publish()

    if remaining == 99:
        assert result.status == "skipped" and result.reason.startswith("space:")
        assert "+ 100 > MaxSpace 3100" in result.reason
        assert ipfs_calls == []
        assert master.chain.submissions == []
        assert master.sub.set_commitment_calls == 0
        assert master.chain.used_space[MASTER.ss58_address] == (index, initial_used)
        assert master.pointer() is None
        assert master.rows() == {}
    else:
        assert result.status == "pending"
        assert ipfs_calls == ["publish", "pin", "read_back"]
        assert len(master.chain.submissions) == 1
        assert master.chain.used_space[MASTER.ss58_address] == (index, 3100)
        assert master.pointer()[1] == cid
        master.chain.advance_to(E1 + 12)
        master.reconcile()
        assert master.rows()[E1][0] == "observed"


def test_dispatch_quota_race_is_failed_and_not_retried(tmp_path, monkeypatch, caplog):
    master = Master(tmp_path, monkeypatch, frozen_epoch_block=E1 - 3 * 360)
    master.prepare_epoch(E1)
    index = master.chain.chain_epoch_index(master.chain.head)
    master.chain.used_space[MASTER.ss58_address] = (index, 2901)
    caplog.set_level("ERROR", logger=bundle_commit_guard.logger.name)

    def publish_after_quota_changes(url, data, **kwargs):
        # Another 100-byte commitment can land after preflight but before inclusion.
        master.chain.used_space[MASTER.ss58_address] = (index, 3001)
        return compute_raw_cid(data)

    monkeypatch.setattr(bundle_publish, "publish_block", publish_after_quota_changes)

    result = master.publish()

    assert result.status == "failed" and "SpaceLimitExceeded" in result.reason
    assert bundle_commit_guard.ALERT_FROZEN in caplog.text
    assert master.chain.used_space[MASTER.ss58_address] == (index, 3001)
    assert master.pointer() is None
    assert master.rows()[E1][0] == "failed"
    assert master.publish().status == "failed"
    assert len(master.chain.submissions) == 1
