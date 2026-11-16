from dataclasses import replace
from types import SimpleNamespace

import pytest

import bundle_consume
from bundle_consume import slave_consume
from bundle_ipfs import IpfsError, compute_raw_cid
from bundle_fetch import RETRY_OFFSETS, fetch_for_schedule
from bundle_store import ArtifactStore, latest_verified
from chain_v2_fake import SLAVE, FakeWallet, FakeMetagraph, config
from test_chain_v2_pointer_lifecycle import Master, E1, E2


@pytest.mark.parametrize('weight_loop', [False, True])
def test_missing_secondary_skips_until_next_publication_then_accepts(tmp_path, monkeypatch, weight_loop):
    m = Master(tmp_path, monkeypatch)
    old = m.prepare_epoch(E1)
    assert m.publish().status == 'pending'
    m.chain.advance_to(E1 + 12)
    cfg = replace(m.cfg, role='slave', state_dir=str(tmp_path / 'slave'), weight_loop_enabled=weight_loop)
    calls = []
    data = {}
    def fetch(cfg, cid):
        calls.append(cid)
        if cid not in data:
            raise IpfsError('all sources: block not found')
        return data[cid]
    monkeypatch.setattr(bundle_consume, '_fetch_envelope', fetch)
    before = len(m.chain.submissions)
    for _ in range(3):
        out = slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)
        assert out.status == 'blocked'
    assert calls == [old]
    # Bounded retries, each spaced by finalized blocks after the first attempt.
    for offset in RETRY_OFFSETS[1:]:
        m.chain.advance_to(E1 + 12 + offset)
        assert slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg).status == 'blocked'
    assert calls == [old] * len(RETRY_OFFSETS)
    assert len(m.chain.submissions) == before
    assert latest_verified(cfg.state_dir, cfg.netuid) is None
    assert ArtifactStore(cfg.state_dir).load(cfg.netuid, E1 // m.chain.tempo, E1) is None
    # Reconstructing config/state on another tick simulates a restart; no obsolete retry.
    out = slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), replace(cfg))
    assert 'waiting for next publication' in out.reason
    assert calls == [old] * len(RETRY_OFFSETS)
    new = m.prepare_epoch(E2)
    data[new] = ArtifactStore(str(m.state)).load(cfg.netuid, E2 // m.chain.tempo, E2)
    assert m.publish().status == 'pending'
    m.chain.advance_to(E2 + 12)
    out = slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)
    assert out.status in ('verified', 'pending', 'applied'), out
    assert calls == [old] * len(RETRY_OFFSETS) + [new]


def test_fetch_crossing_slot_is_not_marked_verified(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.publish()
    m.chain.advance_to(E1 + 12)
    raw = ArtifactStore(str(m.state)).load(m.cfg.netuid, E1 // m.chain.tempo, E1)
    def fetch(cfg, cid):
        m.chain.advance_to(E2 + 12)
        return raw
    monkeypatch.setattr(bundle_consume, '_fetch_envelope', fetch)
    cfg = replace(m.cfg, role='slave', state_dir=str(tmp_path / 'slave'), weight_loop_enabled=True)
    out = slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)
    assert out.status == 'blocked' and 'schedule changed' in out.reason
    assert latest_verified(cfg.state_dir, cfg.netuid) is None


def test_distinct_publication_can_retry_and_stale_head_cannot_reopen_old_failure(tmp_path):
    cfg = config(tmp_path)
    one = SimpleNamespace(epoch_start_block=100, subnet_epoch_index=1)
    two = SimpleNamespace(epoch_start_block=200, subnet_epoch_index=2)
    cid = compute_raw_cid(b'a')
    calls = []
    def missing(cfg, cid):
        calls.append(cid)
        raise IpfsError('missing')
    for schedule in (one, two, one, two):
        with pytest.raises(IpfsError):
            fetch_for_schedule(cfg, cid, schedule, missing)
    assert len(calls) == 2
    assert fetch_for_schedule(cfg, compute_raw_cid(b'b'), two, lambda *a: b'b') == b'b'


def test_unavailable_bookkeeping_error_fails_closed(tmp_path):
    cfg = config(tmp_path)
    (tmp_path / 'unavailable-bundle.sqlite3').write_bytes(b'corrupt')
    with pytest.raises(IpfsError, match='bookkeeping failed'):
        fetch_for_schedule(cfg, compute_raw_cid(b'a'), SimpleNamespace(epoch_start_block=100, subnet_epoch_index=1),
                           lambda *a: pytest.fail('must not fetch without durable skip accounting'))


def test_missing_new_bundle_keeps_existing_verified_vector(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    old = m.prepare_epoch(E1)
    m.publish()
    m.chain.advance_to(E1 + 12)
    raw = ArtifactStore(str(m.state)).load(m.cfg.netuid, E1 // m.chain.tempo, E1)
    cfg = replace(m.cfg, role='slave', state_dir=str(tmp_path / 'slave'), weight_loop_enabled=True)
    monkeypatch.setattr(bundle_consume, '_fetch_envelope', lambda cfg, cid: raw)
    assert slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg).status == 'verified'
    previous = latest_verified(cfg.state_dir, cfg.netuid)
    m.prepare_epoch(E2)
    m.publish()
    m.chain.advance_to(E2 + 12)
    def missing(*a):
        raise IpfsError('missing next bundle')
    monkeypatch.setattr(bundle_consume, '_fetch_envelope', missing)
    assert slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg).status == 'blocked'
    assert latest_verified(cfg.state_dir, cfg.netuid) == previous
    assert compute_raw_cid(ArtifactStore(cfg.state_dir).load(cfg.netuid, E1 // m.chain.tempo, E1)) == old


@pytest.mark.parametrize('gateway_available', [False, True])
def test_all_sources_attempted_before_marking_publication_unavailable(tmp_path, monkeypatch, gateway_available):
    cfg = replace(config(tmp_path), ipfs_api_url='http://127.0.0.1:5001', gateway_urls=('https://example.invalid',))
    schedule = SimpleNamespace(epoch_start_block=100, subnet_epoch_index=1)
    cid = compute_raw_cid(b'bytes')
    calls = []
    def api(*a, **k):
        calls.append('api')
        raise IpfsError('API down')
    def gateway(*a, **k):
        calls.append('gateway')
        if gateway_available:
            return b'bytes'
        raise IpfsError('gateway missing')
    monkeypatch.setattr(bundle_consume, 'fetch_block', api)
    monkeypatch.setattr(bundle_consume, 'fetch_gateway', gateway)
    for _ in range(2):
        if gateway_available:
            assert fetch_for_schedule(cfg, cid, schedule, bundle_consume._fetch_envelope) == b'bytes'
        else:
            with pytest.raises(IpfsError):
                fetch_for_schedule(cfg, cid, schedule, bundle_consume._fetch_envelope)
    assert calls == ['gateway'] * (2 if gateway_available else 1)


def test_crash_during_fetch_does_not_restart_same_publication(tmp_path):
    cfg = config(tmp_path)
    schedule = SimpleNamespace(epoch_start_block=100, subnet_epoch_index=1)
    cid = compute_raw_cid(b'a')
    def crash(*a):
        raise SystemExit('process stopped during network I/O')
    with pytest.raises(SystemExit):
        fetch_for_schedule(cfg, cid, schedule, crash)
    with pytest.raises(IpfsError, match='waiting for next publication'):
        fetch_for_schedule(cfg, cid, schedule, lambda *a: pytest.fail('must not repeat interrupted fetch'))
