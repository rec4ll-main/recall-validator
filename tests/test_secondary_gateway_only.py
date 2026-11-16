from dataclasses import replace
import json
import pytest
import bundle_consume
import bundle_ipfs
from bundle_store import latest_verified
from chain_v2_fake import SLAVE, FakeChain, FakeSubtensor, FakeMetagraph, FakeWallet, build_envelope_for, config, publish_pointer
from environment import _reject_lan_bundle_fetch_url

@pytest.mark.parametrize('url',['http://example.org:5001','https://example.org:5001','https://example.org/api/v0','http://127.0.0.1:8080','https://u:p@example.org'])
def test_admin_and_nonpublic_gateway_rejected(url):
    with pytest.raises(ValueError): _reject_lan_bundle_fetch_url(url)

@pytest.mark.parametrize('case',['valid','malformed','signature','stale','wrong_cid'])
def test_gateway_consume_without_admin(tmp_path, monkeypatch, case):
    e=3600; chain=FakeChain(); sub=FakeSubtensor(chain)
    chain.advance_to(e+1)
    raw=build_envelope_for(chain,sub,e)
    if case=='malformed': raw=b'not json'
    if case=='signature':
        obj=json.loads(raw);obj['signature']='00'*64;raw=json.dumps(obj).encode()
    if case=='stale': raw=build_envelope_for(chain,sub,e-360)
    cid=bundle_ipfs.compute_raw_cid(raw)
    publish_pointer(chain,raw,e+2)
    chain.advance_to(e+12)
    cfg=replace(config(tmp_path),role='slave',gateway_urls=('https://gateway.example',),ipfs_api_url='http://127.0.0.1:5001',weight_loop_enabled=True)
    def admin(*a,**k): pytest.fail('admin endpoint must never be attempted')
    monkeypatch.setattr(bundle_consume,'fetch_block',admin)
    calls=[]
    def request(url,**kwargs):
        calls.append(url)
        assert url=='https://gateway.example/ipfs/'+cid
        assert kwargs.get('method','GET')=='GET'
        return b'changed bytes' if case=='wrong_cid' else raw
    monkeypatch.setattr(bundle_ipfs,'_request',request)
    out=bundle_consume.slave_consume(sub,FakeMetagraph(),FakeWallet(SLAVE),cfg)
    assert len(calls)==1
    assert out.status==('verified' if case=='valid' else 'blocked'),out
    assert (latest_verified(cfg.state_dir,cfg.netuid) is not None)==(case=='valid')
    assert chain.submissions==[]


def test_no_gateway_never_falls_back_to_admin(tmp_path,monkeypatch):
    cfg=replace(config(tmp_path),ipfs_api_url='http://127.0.0.1:5001',role='slave')
    monkeypatch.setattr(bundle_consume,'fetch_block',lambda *a,**k:pytest.fail('admin fallback'))
    with pytest.raises(bundle_ipfs.IpfsError,match='requires public'):
        bundle_consume._fetch_envelope(cfg,bundle_ipfs.compute_raw_cid(b'x'))


def test_secondary_configuration_requires_gateway_not_admin():
    from test_v3_nonmainnet_profile import settings
    assert settings().bundle_ipfs_api_url == ""
    # A configured IPFS API is never contacted by a secondary: ignored, not fatal.
    assert settings(bundle_ipfs_api_url="http://ipfs.example:5001").bundle_ipfs_api_url == ""
    with pytest.raises(ValueError, match="requires public BUNDLE_GATEWAY_URLS"):
        settings(bundle_gateway_urls="")
