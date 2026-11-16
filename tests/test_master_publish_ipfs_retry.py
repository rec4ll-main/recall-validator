"""A transient IPFS failure is retried promptly and boundedly inside the publish window.

Incident: one Kubo timeout made ``master_publish`` return ``error`` and the slot's
pointer was never published before the cutoff D. The IPFS put/pin/read-back now
retries timeouts, connection errors and HTTP 5xx twice (5 s, then 15 s) in the same
call; anything else fails closed at once. The next tick reuses the persisted epoch
envelope (same bytes, same CID) and never rebuilds the allocation or commits twice.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import bundle_ipfs
import bundle_publish
from bundle_ipfs import IpfsError, compute_raw_cid, is_transient, publish_block, retry_transient
from bundle_publish import master_publish
from bundle_store import ArtifactStore
from chain_v2_fake import (
    MASTER,
    NETUID,
    FakeChain,
    FakeMetagraph,
    FakeSubtensor,
    FakeWallet,
    build_envelope_for,
    config,
)

E1 = 3600
URL = "http://127.0.0.1:5001"


def ipfs_error(cause: BaseException) -> IpfsError:
    """The IpfsError the real transport raises for ``cause``."""

    def urlopen(req, timeout):
        raise cause

    with pytest.raises(IpfsError) as info:
        bundle_ipfs._request(URL + "/api/v0/block/put", method="POST", urlopen=urlopen)
    return info.value


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "err", {}, io.BytesIO(b"{}"))


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(bundle_ipfs.time, "sleep", calls.append)
    return calls


class Master:
    def __init__(self, tmp_path, monkeypatch, put_errors=(), pin_errors=()):
        self.chain = FakeChain()
        self.sub = FakeSubtensor(self.chain)
        self.state = tmp_path / "state"
        self.cfg = config(self.state, ipfs_api_url=URL, role="master")
        self.put_errors = list(put_errors)
        self.pin_errors = list(pin_errors)
        self.put_cids: list[str] = []
        self.pinned: list[str] = []

        def put(url, data, **k):
            self.put_cids.append(compute_raw_cid(data))
            if self.put_errors:
                raise self.put_errors.pop(0)
            return compute_raw_cid(data)

        def pin(url, cid, **k):
            if self.pin_errors:
                raise self.pin_errors.pop(0)
            self.pinned.append(cid)

        def never_rebuild(*a, **k):
            raise AssertionError("allocation recomputed for an epoch with a stored envelope")

        monkeypatch.setattr(bundle_publish, "publish_block", put)
        monkeypatch.setattr(bundle_publish, "pin_block", pin)
        monkeypatch.setattr(bundle_publish, "read_back_block", lambda url, cid, data, **k: data)
        monkeypatch.setattr(bundle_publish, "build_envelope", never_rebuild)

    def prepare_epoch(self, e: int) -> str:
        self.chain.advance_to(e + 1)
        env = build_envelope_for(self.chain, self.sub, e)
        self.state.mkdir(parents=True, exist_ok=True)
        ArtifactStore(str(self.state)).store(NETUID, e // self.chain.tempo, e, env)
        return compute_raw_cid(env)

    def publish(self):
        return master_publish(self.sub, FakeMetagraph(), FakeWallet(MASTER), self.cfg)


def test_transient_classification():
    assert is_transient(ipfs_error(TimeoutError("timed out")))
    assert is_transient(ipfs_error(urllib.error.URLError(TimeoutError("timed out"))))
    assert is_transient(ipfs_error(urllib.error.URLError(ConnectionRefusedError())))
    assert is_transient(ipfs_error(ConnectionResetError()))
    assert is_transient(ipfs_error(http_error(500)))
    assert is_transient(ipfs_error(http_error(503)))
    assert not is_transient(ipfs_error(http_error(400)))
    assert not is_transient(ipfs_error(http_error(404)))
    assert not is_transient(ipfs_error(urllib.error.URLError("certificate verify failed")))
    assert not is_transient(IpfsError("returned CID 'x' != recomputed 'y'"))
    assert not is_transient(IpfsError("redirect not allowed (HTTP 302)"))


def test_real_put_timeout_then_success_within_one_call(sleeps):
    data = b"bundle bytes"
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return Resp(json.dumps({"Key": compute_raw_cid(data)}).encode())

    cid = retry_transient(lambda: publish_block(URL, data, urlopen=urlopen))
    assert cid == compute_raw_cid(data)
    assert len(calls) == 2
    assert sleeps == [5.0]


def test_retry_gives_up_after_bound_and_raises_last_error(sleeps):
    attempts = []

    def op():
        attempts.append(1)
        raise ipfs_error(TimeoutError("timed out"))

    with pytest.raises(IpfsError, match="timed out"):
        retry_transient(op)
    assert len(attempts) == 3
    assert sleeps == [5.0, 15.0]


def test_timeout_then_success_publishes_in_the_same_call(tmp_path, monkeypatch, sleeps):
    m = Master(tmp_path, monkeypatch, put_errors=[ipfs_error(TimeoutError("timed out"))])
    cid = m.prepare_epoch(E1)

    out = m.publish()
    assert out.status == "pending", out
    assert out.cid == cid
    assert m.put_cids == [cid, cid]
    assert sleeps == [5.0]
    assert len(m.chain.submissions) == 1


def test_pin_5xx_retries_whole_sequence_with_the_same_cid(tmp_path, monkeypatch, sleeps):
    m = Master(tmp_path, monkeypatch, pin_errors=[ipfs_error(http_error(502))])
    cid = m.prepare_epoch(E1)

    assert m.publish().status == "pending"
    assert m.put_cids == [cid, cid]
    assert m.pinned == [cid]
    assert len(m.chain.submissions) == 1


def test_persistent_timeouts_give_up_then_next_tick_reuses_the_epoch_record(
    tmp_path, monkeypatch, sleeps
):
    m = Master(tmp_path, monkeypatch,
               put_errors=[ipfs_error(TimeoutError("timed out")) for _ in range(3)])
    cid = m.prepare_epoch(E1)

    out = m.publish()
    assert out.status == "error"
    assert out.reason == "IPFS failed: IPFS request failed: timed out"
    assert m.put_cids == [cid, cid, cid]
    assert sleeps == [5.0, 15.0]
    assert m.chain.submissions == []  # fail closed: no pointer without a verified block

    # Next tick, same slot: the stored envelope (never rebuilt) yields the same CID.
    out = m.publish()
    assert out.status == "pending", out
    assert out.cid == cid
    assert set(m.put_cids) == {cid}
    assert len(m.chain.submissions) == 1


def test_client_error_is_not_retried(tmp_path, monkeypatch, sleeps):
    m = Master(tmp_path, monkeypatch, put_errors=[ipfs_error(http_error(400))])
    m.prepare_epoch(E1)

    out = m.publish()
    assert out.status == "error"
    assert out.reason == "IPFS failed: IPFS HTTP 400"
    assert len(m.put_cids) == 1
    assert sleeps == []
    assert m.chain.submissions == []


def test_validation_error_is_not_retried(tmp_path, monkeypatch, sleeps):
    m = Master(tmp_path, monkeypatch, put_errors=[IpfsError("returned CID 'x' != recomputed 'y'")])
    m.prepare_epoch(E1)

    assert m.publish().status == "error"
    assert len(m.put_cids) == 1
    assert sleeps == []
    assert m.chain.submissions == []


def test_no_second_commitment_for_the_same_epoch(tmp_path, monkeypatch, sleeps):
    m = Master(tmp_path, monkeypatch, put_errors=[ipfs_error(TimeoutError("timed out"))])
    cid = m.prepare_epoch(E1)

    assert m.publish().status == "pending"
    again = m.publish()
    assert again.status == "pending"
    assert again.cid == cid
    assert len(m.chain.submissions) == 1
    assert m.put_cids == [cid, cid]  # the resolved journal op never re-enters IPFS
