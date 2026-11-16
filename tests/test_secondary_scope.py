from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import pytest
import bundle_consume
import bundle_publish
import bundle_ipfs
import bundle_rotation
from bundle_fetch import fetch_for_schedule
from bundle_ipfs import IpfsError, compute_raw_cid
from bundle_store import ArtifactStore, latest_verified
from chain_v2_fake import MASTER, SLAVE, FakeWallet, FakeMetagraph, config
from test_chain_v2_pointer_lifecycle import Master, E1

@pytest.mark.parametrize('rotation', [False, True])
@pytest.mark.parametrize('cross_slot', [False, True])
def test_master_retries_same_publication_without_skip_or_cleanup(tmp_path, monkeypatch, rotation, cross_slot):
    m=Master(tmp_path,monkeypatch); cid=m.prepare_epoch(E1); m.publish(); m.chain.advance_to(E1+12)
    raw=ArtifactStore(str(m.state)).load(m.cfg.netuid,E1//m.chain.tempo,E1)
    cfg=replace(m.cfg,state_dir=str(tmp_path/'consumer'),role='master',ipfs_rotation_enabled=rotation,weight_loop_enabled=True)
    def forbidden(*a,**k): pytest.fail('consumer must not open rotation or inspect/remove pins')
    for module,name in ((bundle_publish,'rotation_session'),(bundle_ipfs,'unpin_block'),(bundle_publish,'unpin_block'),(bundle_ipfs,'block_is_pinned'),(bundle_publish,'block_is_pinned')):
        monkeypatch.setattr(module,name,forbidden)
    monkeypatch.setattr(bundle_rotation.Rotation,'cleanup',forbidden)
    calls=[]
    def fetch(*a):
        calls.append(cid)
        if len(calls)==1: raise IpfsError('temporary outage')
        if cross_slot: m.chain.advance_to(E1+m.chain.tempo+12)
        return raw
    monkeypatch.setattr(bundle_consume,'_fetch_envelope',fetch)
    assert bundle_consume.slave_consume(m.sub,FakeMetagraph(),FakeWallet(MASTER),cfg).status=='blocked'
    assert latest_verified(cfg.state_dir,cfg.netuid) is None
    assert bundle_consume.slave_consume(m.sub,FakeMetagraph(),FakeWallet(MASTER),cfg).status=='verified'
    assert calls==[cid,cid]
    assert not (Path(cfg.state_dir)/'unavailable-bundle.sqlite3').exists()
    assert not (Path(cfg.state_dir)/'ipfs-rotation.sqlite3').exists()


def test_secondary_restored_same_publication_still_waits(tmp_path,monkeypatch):
    m=Master(tmp_path,monkeypatch);cid=m.prepare_epoch(E1);m.publish();m.chain.advance_to(E1+12)
    cfg=replace(m.cfg,state_dir=str(tmp_path/'secondary'),role='slave',weight_loop_enabled=True)
    raw=ArtifactStore(str(m.state)).load(cfg.netuid,E1//m.chain.tempo,E1)
    calls=[]
    def fetch(*a):
        calls.append(cid)
        if len(calls)==1:raise IpfsError('temporary all-source failure')
        return raw
    monkeypatch.setattr(bundle_consume,'_fetch_envelope',fetch)
    for _ in range(3): assert bundle_consume.slave_consume(m.sub,FakeMetagraph(),FakeWallet(SLAVE),cfg).status=='blocked'
    assert calls==[cid]
    assert latest_verified(cfg.state_dir,cfg.netuid) is None


def test_secondary_later_publication_same_cid_recovers(tmp_path):
    cfg=replace(config(tmp_path),role='slave');cid=compute_raw_cid(b'same')
    first=SimpleNamespace(epoch_start_block=100,subnet_epoch_index=1)
    later=SimpleNamespace(epoch_start_block=200,subnet_epoch_index=2)
    def down(*a): raise IpfsError('temporary all-source failure')
    with pytest.raises(IpfsError):fetch_for_schedule(cfg,cid,first,down)
    with pytest.raises(IpfsError,match='waiting for next publication'):fetch_for_schedule(cfg,cid,first,lambda *a:b'same')
    assert fetch_for_schedule(cfg,cid,later,lambda *a:b'same')==b'same'
