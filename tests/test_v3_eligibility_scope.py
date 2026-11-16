"""Allocator eligibility scope (empty or grounded-v1) and its per-artifact binding."""
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from chain_v2_fake import OWNER, MASTER, FakeChain, FakeMetagraph, FakeSubtensor, FakeWallet, config
from test_incentive_v3_integration import _body, _cohort
from v3_eligibility import EligibilityScopeError, eligibility_state_dir
from v3_metrics_client import V3MetricsError, fetch_v3_metrics, parse_v3_metrics

EPOCH = 3600


@pytest.mark.parametrize("meta", [{"contract": "synthetic-chain-v1"}, None, {}])
def test_benchmark_metadata_in_evidence_is_refused(meta):
    body = _body(epoch=EPOCH); body["generation_benchmark"] = meta
    with pytest.raises(V3MetricsError, match="not accepted"):
        parse_v3_metrics(body, body["netuid"], EPOCH, _cohort())


def test_transport_rejects_duplicate_json_keys():
    raw = json.dumps(_body(epoch=EPOCH)).replace('"demand":', '"demand": {}, "demand":', 1)
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    body = _body(epoch=EPOCH)
    with pytest.raises(V3MetricsError, match="JSON"):
        fetch_v3_metrics("https://api.example.invalid", "fixture-key", [(u, h) for h, u in _cohort().items()],
            body["netuid"], EPOCH, urlopen=lambda *a, **kw: Response(raw.encode()))


def test_state_dir_by_scope(tmp_path):
    root = tmp_path / "v3_eligibility" / "weights"
    assert eligibility_state_dir(str(tmp_path), "weights", "") == str(tmp_path)
    assert eligibility_state_dir(str(tmp_path), "weights", "grounded-v1") == str(root / "grounded-v1")
    for bad in ("a" * 64, "other"):
        with pytest.raises(EligibilityScopeError):
            eligibility_state_dir(str(tmp_path), "weights", bad)
    with pytest.raises(EligibilityScopeError):
        eligibility_state_dir(str(tmp_path), "shadow", "")
    (root / ("benchmark-" + "a" * 64)).mkdir(parents=True)
    with pytest.raises(EligibilityScopeError, match="grounded-v1"):
        eligibility_state_dir(str(tmp_path), "weights", "")
    assert eligibility_state_dir(str(tmp_path), "weights", "grounded-v1") == str(root / "grounded-v1")


class World:
    def __init__(self, tmp, monkeypatch, scope):
        import bundle_publish, v3_runtime
        from bundle_ipfs import compute_raw_cid
        self.cfg = replace(config(tmp / "state", role="master", ipfs_api_url="http://127.0.0.1:5001"),
                           v3_eligibility_scope=scope)
        self.chain = FakeChain(head=EPOCH + 5); self.sub = FakeSubtensor(self.chain); self.calls = []
        self.schedule = SimpleNamespace(epoch_start_block=EPOCH, subnet_epoch_index=EPOCH // 360,
                                        cutoff_block=EPOCH + 12)
        monkeypatch.setattr(v3_runtime, "fetch_v3_metrics", self.fetch)
        monkeypatch.setattr(bundle_publish, "publish_block", lambda url, data, **kw: compute_raw_cid(data))
        monkeypatch.setattr(bundle_publish, "pin_block", lambda *a, **kw: None)
        monkeypatch.setattr(bundle_publish, "read_back_block", lambda url, cid, data, **kw: data)

    def fetch(self, url, key, cohort, netuid, epoch, **kwargs):
        self.calls.append(epoch)
        return parse_v3_metrics(_body(epoch=epoch, netuid=netuid), netuid, epoch, {h: u for u, h in cohort})

    def compute(self, cfg=None):
        from v3_runtime import compute_v3_epoch
        return compute_v3_epoch(self.sub, FakeMetagraph(), FakeWallet(MASTER), cfg or self.cfg,
            self.schedule, fetch=self.fetch, resolve_burn=lambda *a: (0, OWNER.ss58_address))


@pytest.mark.parametrize("scope", ["", "grounded-v1"])
def test_record_and_state_keep_their_format_and_directory(tmp_path, monkeypatch, scope):
    from v3_state import load_state
    w = World(tmp_path, monkeypatch, scope)
    rec, _, _ = w.compute()
    assert rec["generation_benchmark_policy_sha256"] == ""
    assert "generation_benchmark" not in rec["inputs"]
    state_dir = eligibility_state_dir(w.cfg.state_dir, "weights", scope)
    assert load_state(state_dir, "weights")["generation_benchmark_policy_sha256"] == ""
    assert w.compute()[0] == rec and w.calls == [EPOCH]


def test_stored_state_from_a_benchmark_scope_is_refused(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, "")
    w.compute()
    p = Path(w.cfg.state_dir) / "v3/weights/epochs/3600.json"
    rec = json.loads(p.read_text()); rec["generation_benchmark_policy_sha256"] = "a" * 64
    p.write_text(json.dumps(rec))
    with pytest.raises(EligibilityScopeError):
        w.compute()


def test_grounded_scope_binds_each_artifact_and_publish_requires_it(tmp_path, monkeypatch):
    from bundle_ipfs import IpfsError
    from bundle_publish import master_publish
    import bundle_publish
    w = World(tmp_path, monkeypatch, "grounded-v1")
    def failed_ipfs(*a, **kw): raise IpfsError("fixture unavailable")
    monkeypatch.setattr(bundle_publish, "publish_block", failed_ipfs)
    out = master_publish(w.sub, FakeMetagraph(), FakeWallet(MASTER), w.cfg)
    assert out.status == "error" and not w.chain.submissions
    bindings = list((Path(w.cfg.state_dir) / "v3_artifact_policies").glob("*.json"))
    assert len(bindings) == 1
    obj = json.loads(bindings[0].read_text())
    assert obj["eligibility_scope"] == "grounded-v1" and obj["generation_benchmark"] is None
    bindings[0].unlink()
    from bundle_ipfs import compute_raw_cid
    monkeypatch.setattr(bundle_publish, "publish_block", lambda url, data, **kw: compute_raw_cid(data))
    out = master_publish(w.sub, FakeMetagraph(), FakeWallet(MASTER), w.cfg)
    assert out.status == "blocked" and "binding missing" in out.reason
    assert not w.chain.submissions
