"""Bounded, durably counted secondary fetch retries per publication."""

from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

import bundle_consume
import bundle_fetch
import bundle_ipfs
from bundle_consume import slave_consume
from bundle_fetch import MAX_ATTEMPTS, RETRY_OFFSETS, fetch_for_schedule
from bundle_ipfs import IpfsError, compute_raw_cid
from bundle_store import ArtifactStore, latest_verified
from chain_v2_fake import SLAVE, FakeMetagraph, FakeWallet, config
from test_chain_v2_pointer_lifecycle import E1, E2, Master

ONE = SimpleNamespace(epoch_start_block=100, subnet_epoch_index=1)
TWO = SimpleNamespace(epoch_start_block=200, subnet_epoch_index=2)
CID = compute_raw_cid(b'a')


class Source:
    """Fake fetch: fails ``fail`` times, then returns ``data``; records each call."""

    def __init__(self, fail=0, data=b'a', exc=None):
        self.fail, self.data, self.exc, self.calls = fail, data, exc, []

    def __call__(self, cfg, cid):
        self.calls.append(tuple(cfg.gateway_urls))
        if len(self.calls) <= self.fail:
            raise self.exc or IpfsError('transient gateway error')
        return self.data


def attempt(cfg, source, head, schedule=ONE, cid=CID):
    return fetch_for_schedule(cfg, cid, schedule, source, head_block=head)


def test_retry_succeeds_on_second_attempt_after_spacing(tmp_path):
    cfg, source = config(tmp_path), Source(fail=1)
    with pytest.raises(IpfsError, match='transient'):
        attempt(cfg, source, 1000)
    for head in (1000, 1000 + RETRY_OFFSETS[1] - 1):
        with pytest.raises(IpfsError, match=f'not due until finalized block {1000 + RETRY_OFFSETS[1]}'):
            attempt(cfg, source, head)
    assert len(source.calls) == 1
    assert attempt(cfg, source, 1000 + RETRY_OFFSETS[1]) == b'a'
    assert len(source.calls) == 2


def test_budget_exhausted_after_three_attempts_and_stays_failed(tmp_path):
    cfg, source = config(tmp_path), Source(fail=99)
    for offset in RETRY_OFFSETS:
        with pytest.raises(IpfsError, match='transient'):
            attempt(cfg, source, 1000 + offset)
    assert len(source.calls) == MAX_ATTEMPTS == 3
    for head in (1000 + RETRY_OFFSETS[-1], 5000, 10 ** 9):
        with pytest.raises(IpfsError, match='waiting for next publication'):
            attempt(cfg, source, head)
    # A restart (fresh config object) keeps the exhausted budget.
    with pytest.raises(IpfsError, match='waiting for next publication'):
        attempt(replace(cfg), source, 10 ** 9)
    assert len(source.calls) == 3


def test_crash_after_count_spends_the_attempt(tmp_path):
    cfg = config(tmp_path)
    crash = Source(fail=99, exc=SystemExit('process stopped during network I/O'))
    with pytest.raises(SystemExit):
        attempt(cfg, crash, 1000)
    with pytest.raises(IpfsError, match='not due'):
        attempt(cfg, crash, 1001)
    for offset in RETRY_OFFSETS[1:]:
        with pytest.raises(SystemExit):
            attempt(cfg, crash, 1000 + offset)
    assert len(crash.calls) == 3
    with pytest.raises(IpfsError, match='waiting for next publication'):
        attempt(cfg, Source(), 5000)


def test_stale_head_cannot_reopen_older_failed_publication(tmp_path):
    cfg = config(tmp_path)
    old, new = Source(fail=99), Source(fail=99)
    with pytest.raises(IpfsError):
        attempt(cfg, old, 1000, ONE)
    with pytest.raises(IpfsError):
        attempt(cfg, new, 1010, TWO, compute_raw_cid(b'b'))
    # Budget remains on ONE, but a newer publication was already attempted.
    with pytest.raises(IpfsError, match='waiting for next publication'):
        attempt(cfg, Source(), 5000, ONE)
    assert len(old.calls) == 1
    # Without a finalized head no retry can be spaced: first attempt only.
    with pytest.raises(IpfsError, match='waiting for next publication'):
        fetch_for_schedule(cfg, compute_raw_cid(b'b'), TWO, Source())


def test_rotation_stays_within_configured_gateways(tmp_path, monkeypatch):
    gateways = ('https://gw-a.example', 'https://gw-b.example', 'https://gw-c.example')
    cfg = replace(config(tmp_path), role='slave', gateway_urls=gateways,
                  ipfs_api_url='http://127.0.0.1:5001')
    monkeypatch.setattr(bundle_consume, 'fetch_block', lambda *a, **k: pytest.fail('IPFS API used'))
    urls = []
    def request(url, **kwargs):
        urls.append(url)
        raise IpfsError('gateway down')
    monkeypatch.setattr(bundle_ipfs, '_request', request)
    for offset in RETRY_OFFSETS:
        with pytest.raises(IpfsError):
            attempt(cfg, bundle_consume._fetch_envelope, 1000 + offset)
    bases = [u.rsplit('/ipfs/', 1)[0] for u in urls]
    assert set(bases) == set(gateways) and len(bases) == 9
    assert [bases[0], bases[3], bases[6]] == list(gateways)  # each attempt starts one further
    assert all(u.endswith('/ipfs/' + CID) for u in urls)


def test_rotation_never_passes_a_source_outside_the_list(tmp_path):
    gateways = ('https://gw-a.example', 'https://gw-b.example')
    cfg = replace(config(tmp_path), gateway_urls=gateways)
    source = Source(fail=99)
    for offset in RETRY_OFFSETS:
        with pytest.raises(IpfsError):
            attempt(cfg, source, 1000 + offset)
    assert source.calls == [gateways, gateways[::-1], gateways]


def test_lan_gateway_still_refused_on_every_attempt(tmp_path, monkeypatch):
    cfg = replace(config(tmp_path), role='slave', gateway_urls=('http://127.0.0.1:8080',))
    monkeypatch.setattr(bundle_ipfs, '_request', lambda *a, **k: pytest.fail('LAN fetched'))
    for offset in RETRY_OFFSETS:
        with pytest.raises(IpfsError, match='invalid secondary public gateway'):
            attempt(cfg, bundle_consume._fetch_envelope, 1000 + offset)


def test_bookkeeping_error_fails_closed_without_fetch(tmp_path):
    cfg = config(tmp_path)
    (tmp_path / 'unavailable-bundle.sqlite3').write_bytes(b'corrupt')
    with pytest.raises(IpfsError, match='bookkeeping failed'):
        attempt(cfg, lambda *a: pytest.fail('fetched without durable count'), 1000)
    with pytest.raises(IpfsError, match='bookkeeping failed'):
        bundle_fetch.mark_rejected(cfg, CID, ONE)


def test_legacy_single_attempt_row_counts_as_one_attempt(tmp_path):
    cfg = config(tmp_path)
    scope = json.dumps([cfg.genesis_hash, cfg.netuid, cfg.master_hotkey])
    with sqlite3.connect(tmp_path / 'unavailable-bundle.sqlite3') as db:
        db.execute('''CREATE TABLE unavailable (scope TEXT NOT NULL, epoch INTEGER NOT NULL,
            seq INTEGER NOT NULL, cid TEXT NOT NULL, PRIMARY KEY(scope,epoch,seq,cid))''')
        db.execute('INSERT INTO unavailable VALUES (?,?,?,?)', (scope, 100, 1, CID))
    source = Source(fail=99)
    with pytest.raises(IpfsError, match=f'not due until finalized block {2000 + RETRY_OFFSETS[1]}'):
        attempt(cfg, source, 2000)
    for offset in RETRY_OFFSETS[1:]:
        with pytest.raises(IpfsError, match='transient'):
            attempt(cfg, source, 2000 + offset)
    with pytest.raises(IpfsError, match='waiting for next publication'):
        attempt(cfg, source, 9999)
    assert len(source.calls) == 2


def _slave(m, tmp_path):
    return replace(m.cfg, role='slave', state_dir=str(tmp_path / 'slave'), weight_loop_enabled=True)


def test_consume_retries_transient_failure_then_verifies(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.publish()
    m.chain.advance_to(E1 + 12)
    raw = ArtifactStore(str(m.state)).load(m.cfg.netuid, E1 // m.chain.tempo, E1)
    source = Source(fail=1, data=raw)
    monkeypatch.setattr(bundle_consume, '_fetch_envelope', source)
    verified = []
    real_verify = bundle_consume.verify_bundle
    monkeypatch.setattr(bundle_consume, 'verify_bundle',
                        lambda *a, **k: verified.append(1) or real_verify(*a, **k))
    cfg = _slave(m, tmp_path)
    assert slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg).status == 'blocked'
    assert 'not due' in slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg).reason
    m.chain.advance_to(E1 + 12 + RETRY_OFFSETS[1])
    out = slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)
    assert out.status == 'verified', out
    assert len(source.calls) == 2 and verified == [1]
    assert latest_verified(cfg.state_dir, cfg.netuid) is not None


def test_verification_failure_is_never_retried_into_acceptance(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.publish()
    m.chain.advance_to(E1 + 12)
    raw = ArtifactStore(str(m.state)).load(m.cfg.netuid, E1 // m.chain.tempo, E1)
    obj = json.loads(raw)
    obj['signature'] = '00' * 64
    tampered = json.dumps(obj).encode()
    calls = []
    def fetch(cfg, cid):
        calls.append(cid)
        if len(calls) == 1:
            raise IpfsError('transient')
        return tampered if len(calls) == 2 else raw
    monkeypatch.setattr(bundle_consume, '_fetch_envelope', fetch)
    cfg = _slave(m, tmp_path)
    assert slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg).status == 'blocked'
    m.chain.advance_to(E1 + 12 + RETRY_OFFSETS[1])
    out = slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)
    assert out.status == 'blocked' and 'verification failed' in out.reason
    # Retry budget remains, but rejected bytes close the publication for good.
    m.chain.advance_to(E1 + 12 + RETRY_OFFSETS[2])
    out = slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)
    assert out.status == 'blocked' and 'failed verification' in out.reason
    assert len(calls) == 2
    assert latest_verified(cfg.state_dir, cfg.netuid) is None


def test_rejection_bookkeeping_failure_still_blocks(tmp_path, monkeypatch):
    m = Master(tmp_path, monkeypatch)
    m.prepare_epoch(E1)
    m.publish()
    m.chain.advance_to(E1 + 12)
    monkeypatch.setattr(bundle_consume, '_fetch_envelope', lambda cfg, cid: b'not json')
    def broken(*a):
        raise IpfsError('rejected-publication bookkeeping failed')
    monkeypatch.setattr(bundle_consume, 'mark_rejected', broken)
    cfg = _slave(m, tmp_path)
    out = slave_consume(m.sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)
    assert out.status == 'blocked' and 'bookkeeping failed' in out.reason
    assert latest_verified(cfg.state_dir, cfg.netuid) is None
