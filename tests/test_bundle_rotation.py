from dataclasses import replace
import io
import json
import multiprocessing
import urllib.error

import pytest
from bundle_ipfs import compute_raw_cid, block_is_pinned, unpin_block, IpfsError
from bundle_rotation import Rotation, rotation_session
from bundle_runtime import BundleConfig




def test_lost_ledger_identity_and_corruption_fail_closed(tmp_path):
    r = Rotation(tmp_path, 'owner')
    r.close()
    with pytest.raises(ValueError, match='identity'):
        Rotation(tmp_path, 'other')
    (tmp_path / 'ipfs-rotation.sqlite3').unlink()
    with pytest.raises(ValueError, match='lost'):
        Rotation(tmp_path, 'owner')


def test_prepare_intent_survives_crash_before_pin(tmp_path):
    r = Rotation(tmp_path, 'owner')
    r.prepare(b'a', lambda c: False)
    r.close()
    r = Rotation(tmp_path, 'owner')
    r.prepare(b'a', lambda c: pytest.fail('must reuse durable ownership'))
    assert r.db.execute('SELECT owned FROM artifacts').fetchone() == (1,)
    r.close()


def _take_lock(cfg, ready, acquired):
    ready.set()
    with rotation_session(cfg):
        acquired.set()


def test_process_serialization(tmp_path):
    cfg = BundleConfig(1, 'genesis', 'master', str(tmp_path), 'master', ipfs_rotation_enabled=True)
    ctx = multiprocessing.get_context('spawn')
    ready, acquired = ctx.Event(), ctx.Event()
    with rotation_session(cfg):
        p = ctx.Process(target=_take_lock, args=(cfg, ready, acquired))
        p.start()
        assert ready.wait(3)
        assert not acquired.wait(.1)
    p.join(10)
    if p.is_alive():
        p.terminate()
        p.join()
        pytest.fail("lock never released")
    assert p.exitcode == 0
    assert acquired.is_set()


@pytest.mark.parametrize('prefix', ['', '/ipfs/'])
def test_cid_targeted_rpc_and_missing_pin(prefix):
    cid = compute_raw_cid(b'a')
    urls = []
    def missing(req, **kw):
        urls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 500, 'error', {},
            io.BytesIO(json.dumps({'Message': f"path '{prefix}{cid}' is not pinned"}).encode()))
    assert not block_is_pinned('http://localhost:5001', cid, urlopen=missing)
    assert urls == [f'http://localhost:5001/api/v0/pin/ls?arg={cid}&type=all']
    def offline(req, **kw):
        raise urllib.error.HTTPError(req.full_url, 500, 'error', {}, io.BytesIO(b'{"Message":"offline"}'))
    with pytest.raises(IpfsError):
        block_is_pinned('http://localhost:5001', cid, urlopen=offline)
    for fn in (block_is_pinned, unpin_block):
        with pytest.raises(IpfsError):
            fn('http://localhost:5001', '../bad', urlopen=missing)


def test_successful_pin_status_and_removal_rpc_are_cid_scoped():
    cid = compute_raw_cid(b'a')
    calls = []
    def response(req, **kw):
        calls.append((req.method, req.full_url))
        body = {'Keys': {cid: {'Type': 'recursive'}} if 'pin/ls?' in req.full_url else {}, 'Pins': [cid]}
        return io.BytesIO(json.dumps(body).encode())
    assert block_is_pinned('http://localhost:5001', cid, urlopen=response)
    unpin_block('http://localhost:5001', cid, urlopen=response)
    assert calls == [('POST', f'http://localhost:5001/api/v0/pin/ls?arg={cid}&type=all'),
                     ('POST', f'http://localhost:5001/api/v0/pin/rm?arg={cid}')]


def publish(r, pins, data, epoch):
    cid = compute_raw_cid(data)
    r.prepare(data, pins.__contains__)
    pins.add(cid)
    r.observed(cid, epoch)
    return cid


def test_latest_three_distinct_publications_and_repeated_cid(tmp_path):
    r = Rotation(tmp_path, 'owner')
    pins = set()
    a, b, c = [publish(r, pins, x, i) for i, x in enumerate((b'a', b'b', b'c'))]
    r.cleanup(pins.__contains__, pins.remove, lambda c: None)
    assert pins == {a, b, c}
    publish(r, pins, b'a', 3)  # refresh recency, no fourth distinct CID
    r.cleanup(pins.__contains__, pins.remove, lambda c: None)
    assert pins == {a, b, c}
    d = publish(r, pins, b'd', 4)
    r.cleanup(pins.__contains__, pins.remove, lambda c: None)
    assert pins == {a, c, d}  # b, not the filename/oldest insertion a, is evicted
    r.close()


def test_restart_partial_cleanup_and_missing_pin_retries(tmp_path):
    r = Rotation(tmp_path, 'owner')
    pins = set()
    cids = [publish(r, pins, bytes([i]), i) for i in range(5)]
    def unpin(cid):
        pins.remove(cid)
        if cid == cids[0]:
            raise IpfsError('response lost after removal')
    assert list(r.cleanup(pins.__contains__, unpin, lambda c: None)) == [cids[0]]
    r.close()
    r = Rotation(tmp_path, 'owner')
    assert r.cleanup(pins.__contains__, lambda c: pytest.fail('already absent'), lambda c: None) == {}
    assert pins == set(cids[-3:])
    r.close()


def test_unmanaged_and_pending_are_not_removed_or_counted_as_successes(tmp_path):
    r = Rotation(tmp_path, 'owner')
    pins = {compute_raw_cid(b'foreign')}
    foreign = publish(r, pins, b'foreign', 0)
    cids = [publish(r, pins, bytes([i]), i+1) for i in range(4)]
    # Old CID is in flight again: its prior successful lifetime cannot authorize removal.
    r.prepare(bytes([0]), pins.__contains__)
    pending = compute_raw_cid(b'pending')
    r.prepare(b'pending', pins.__contains__)
    pins.add(pending)
    r.cleanup(pins.__contains__, lambda c: pytest.fail('protected'), lambda c: None)
    assert pins == {foreign, pending, *cids}
    r.close()


def test_missing_or_corrupt_replacement_blocks_all_cleanup(tmp_path):
    r = Rotation(tmp_path, 'owner')
    pins = set()
    cids = [publish(r, pins, bytes([i]), i) for i in range(4)]
    pins.remove(cids[-1])
    assert cids[-1] in r.cleanup(pins.__contains__, lambda c: pytest.fail('must retain'), lambda c: None)
    pins.add(cids[-1])
    def bad(cid):
        raise IpfsError('bad readback')
    assert cids[-1] in r.cleanup(pins.__contains__, lambda c: pytest.fail('must retain'), bad)
    r.close()


def test_historical_reconciliation_does_not_reorder_success(tmp_path):
    r = Rotation(tmp_path, 'owner')
    pins = set()
    cids = [publish(r, pins, bytes([i]), i * 100) for i in range(4)]
    r.observed(cids[0], 0)
    with pytest.raises(ValueError, match='conflicting'):
        r.observed(cids[2], 100)
    r.cleanup(pins.__contains__, pins.remove, lambda c: None)
    assert pins == set(cids[-3:])
    r.close()


@pytest.mark.parametrize('failure', ['pin', 'verify', 'chain', 'ambiguous'])
def test_failed_fourth_publication_keeps_previous_three(tmp_path, monkeypatch, failure):
    import bundle_publish
    from test_chain_v2_pointer_lifecycle import Master, E1
    m = Master(tmp_path, monkeypatch)
    m.cfg = replace(m.cfg, ipfs_rotation_enabled=True)
    pins = set()
    monkeypatch.setattr(bundle_publish, 'block_is_pinned', lambda url, cid, **k: cid in pins)
    monkeypatch.setattr(bundle_publish, 'pin_block', lambda url, cid, **k: pins.add(cid))
    monkeypatch.setattr(bundle_publish, 'fetch_block', lambda *a, **k: b'verified')
    monkeypatch.setattr(bundle_publish, 'unpin_block', lambda *a, **k: pytest.fail('must retain'))
    old = []
    for i in range(3):
        epoch = E1 + i * 360
        old.append(m.prepare_epoch(epoch))
        assert m.publish().status == 'pending'
        m.chain.advance_to(epoch + 12)
        m.reconcile()
    epoch = E1 + 3 * 360
    m.prepare_epoch(epoch)
    def fail(*a, **k):
        raise IpfsError('failure')
    if failure in ('chain', 'ambiguous'):
        m.chain.submit_behaviors = ['fail' if failure == 'chain' else 'timeout']
    else:
        monkeypatch.setattr(bundle_publish, 'pin_block' if failure == 'pin' else 'read_back_block', fail)
    assert m.publish().status in ('error', 'failed', 'unknown')
    m.chain.advance_to(epoch + 12)
    m.reconcile()
    assert set(old) <= pins
