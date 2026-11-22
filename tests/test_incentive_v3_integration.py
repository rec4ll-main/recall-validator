"""Incentive v3 (score_version 11) integration tests.

probes -> scores -> allocation -> weights, all in-memory: the shared web fixture is
parsed by the v3 evidence client, allocated by allocation_v3, signed as a v11 envelope
by the master (INCENTIVE_V3_MODE=weights), and applied by slave_consume on the
deterministic fake chain. Also: score_version 11 is the only version, single-setter
guards, codec version isolation, upgrade recovery of persisted v10 envelopes.
"""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import bundle_consume
import score_version
from bundle_consume import slave_consume
from bundle_ipfs import IpfsError, compute_raw_cid
from bundle_schedule import PUBLICATION_CUTOFF_BLOCKS
from bundle_v2 import parse_bundle, parse_bundle_for_journal_recovery
from bundle_v2_types import BundleCodecError
from chain_v2_fake import (
    GENESIS, HOTKEYS, MASTER, MINERS, NETUID, SLAVE, FakeChain, FakeMetagraph, FakeSubtensor,
    FakeWallet, build_envelope_for, config, publish_pointer,
)
from environment import EnvironmentVariables
from allocation_v3 import allocate_v3
from v3_metrics_client import V3MetricsError, fetch_v3_metrics, parse_v3_metrics, to_miner_evidence
from v3_state import load_epoch_record, load_state

FIXTURE = Path(__file__).parent / "fixtures" / "recall_v3_metrics_fixture.json"
E1 = 3600


def _body(epoch=E1, netuid=NETUID):
    body = json.loads(FIXTURE.read_text())["body"]
    body["netuid"], body["epoch_start_block"] = netuid, epoch
    for i, m in enumerate(body["miners"]):
        m["hotkey"] = MINERS[i].ss58_address
    return body


def _cohort():
    return {MINERS[i].ss58_address: 3 + i for i in range(4)}

# ------------------------------------------------------------------ flags / env

def _env(**kw):
    base = dict(bundle_mode="chain-v2", bundle_role="master", allocation_mode="v2",
                allocation_expected_genesis_hash="0xgenesis", master_validator_hotkey_ss58="5Master",
                bundle_ipfs_api_url="http://127.0.0.1:5001", network="test", netuid=353, _env_file=None)
    base.update(kw)
    return EnvironmentVariables(**base)


def test_score_version_11_is_the_only_version():
    assert score_version.SUPPORTED_SCORE_VERSION == score_version.active_score_version() == "11"
    assert score_version.active_profile() == ("11", "readiness_service_v3", 21600)
    assert not hasattr(score_version, "activate_score_version_11")
    assert _env().incentive_v3_mode is None

# ------------------------------------------------------------------ evidence client

def test_fixture_parses():
    ev = parse_v3_metrics(_body(), NETUID, E1, _cohort())
    assert ev.t_call.endswith("Z") and len(ev.miners) == 4 and ev.demand["vdb_query"] == 120.0


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(score_version="10"),
    lambda b: b.update(storage_rewards_enabled=True),
    lambda b: b.update(slot_window_seconds=86400),
    lambda b: b.update(t_call="2026-10-08 04:12:00"),
    lambda b: b["demand"].pop("gen"),
    lambda b: b["miners"][0]["tiers"]["vdb"]["checks"].append({"name": "emb_quality", "n_eff": 1, "p_hat": 1}),
    lambda b: b["miners"][0]["tiers"]["vdb"]["checks"][0].update(p_hat=1.2),
    lambda b: b["miners"][0]["tiers"]["vdb"].update(h_passes=10_000),
    lambda b: b["miners"][0].update(hotkey=SLAVE.ss58_address),
    lambda b: b["miners"][0]["tiers"]["vdb"].update(credited={"emb": 1.0}),
    lambda b: b["miners"].append(copy.deepcopy(b["miners"][0])),
    # A verdict the API left out is never read in the miner's favour.
    lambda b: b["miners"][0].pop("serious_failure"),
    lambda b: b["miners"][0]["tiers"]["vdb"].pop("factor"),
    lambda b: b["miners"][0]["tiers"]["vdb"].pop("h_passes"),
    lambda b: b["miners"][0]["tiers"]["vdb"].pop("h_n"),
])
def test_evidence_validation_fails_closed(mutate):
    b = _body()
    mutate(b)
    with pytest.raises(V3MetricsError):
        parse_v3_metrics(b, NETUID, E1, _cohort())


def test_fetch_transport_rules():
    with pytest.raises(V3MetricsError):
        fetch_v3_metrics("http://api.example.com", "k", [], NETUID, E1)    # plaintext public
    with pytest.raises(V3MetricsError):
        fetch_v3_metrics("https://api.example.com", "", [], NETUID, E1)    # no key
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout):
        seen["url"], seen["key_present"] = req.full_url, bool(req.headers.get("X-internal-api-key"))
        return Resp(json.dumps(_body()).encode())

    cohort = [(u, h) for h, u in _cohort().items()]
    ev = fetch_v3_metrics("https://api.example.com/api", "secret", cohort, NETUID, E1, urlopen=opener)
    assert seen["url"] == "https://api.example.com/api/validator/v3/metrics" and seen["key_present"]
    assert len(ev.miners) == 4

# ------------------------------------------------------------------ codec isolation

def _v10_envelope_from(env_bytes: bytes) -> bytes:
    """Authentic bytes a score_version 10 master signed; only recovery may read them."""
    from bundle_v2 import signing_message
    payload = json.loads(env_bytes)["payload"]
    payload.update(score_version="10", score_kind="window_quality_v1", evidence_window_seconds=3600)
    sig = MASTER.sign(signing_message(payload)).hex()
    return json.dumps({"payload": payload, "signature": sig}, ensure_ascii=True,
                      sort_keys=True, separators=(",", ":")).encode()


def test_codec_v11_process_rejects_v10_and_recovery_reads_it():
    chain = FakeChain(); sub = FakeSubtensor(chain); chain.advance_to(E1 + 5)
    base = build_envelope_for(chain, sub, E1)
    assert parse_bundle(base).payload.score_version == "11"
    v10 = _v10_envelope_from(base)
    with pytest.raises(BundleCodecError, match="unsupported score version '10'"):
        parse_bundle(v10)                                    # v11 process refuses v10
    rec = parse_bundle_for_journal_recovery(v10)             # upgrade can still settle it
    assert (rec.payload.score_kind, rec.payload.evidence_window_seconds) == ("window_quality_v1", 3600)
    mixed = json.loads(base)["payload"]
    mixed.update(score_version="10")                         # v10 label on the v11 profile
    from bundle_v2 import sign_bundle
    with pytest.raises(BundleCodecError):
        sign_bundle(mixed, MASTER)

# ------------------------------------------------------------------ runtime (fake chain)

class World:
    def __init__(self, tmp_path, monkeypatch):
        self.chain = FakeChain(); self.sub = FakeSubtensor(self.chain)
        self.tmp = tmp_path; self.blocks = {}
        self.fetch_calls = 0

        def fake_fetch(api_url, cid, **kw):
            data = self.blocks.get(cid)
            if data is None or compute_raw_cid(data) != cid:
                raise IpfsError("IPFS HTTP 404")
            return data
        monkeypatch.setattr(bundle_consume, "fetch_block", fake_fetch)
        monkeypatch.setattr(bundle_consume, "fetch_gateway", fake_fetch)

    def evidence(self, api_url, key, cohort, netuid, epoch, allow_plaintext=False):
        self.fetch_calls += 1
        assert MASTER.ss58_address not in [h for _, h in cohort]
        return parse_v3_metrics(_body(epoch), netuid, epoch, {h: u for u, h in cohort})

    def cfg(self, role="master"):
        c = config(self.tmp / role, ipfs_api_url="http://127.0.0.1:5001", role=role)
        from dataclasses import replace
        return replace(c, api_url="https://api.test", internal_api_key="k")

    def schedule(self, e):
        return SimpleNamespace(epoch_start_block=e, subnet_epoch_index=e // self.chain.tempo,
                               cutoff_block=e + PUBLICATION_CUTOFF_BLOCKS)


def test_end_to_end_v11_master_sign_then_slave_applies(tmp_path, monkeypatch):
    """probes (fixture) -> scores -> v3 allocation -> signed v11 bundle -> slave set_weights."""
    import v3_runtime
    from bundle_envelope import build_envelope
    w = World(tmp_path, monkeypatch)
    w.chain.advance_to(E1 + 5)
    monkeypatch.setattr(v3_runtime, "fetch_v3_metrics", w.evidence)
    env = build_envelope(w.sub, FakeMetagraph(), FakeWallet(MASTER), w.cfg(), w.schedule(E1), fetch=None)
    b = parse_bundle(env).payload
    assert (b.score_version, b.score_kind, b.evidence_window_seconds) == ("11", "readiness_service_v3", 21600)
    assert 0 not in b.uids and 6 not in b.uids               # never burns; serious miner unpaid
    shares = [v / sum(b.weights_u16) for v in b.weights_u16]
    assert abs(sum(shares) - 1.0) < 1e-9 and all(s > 0 for s in shares)  # quiet network: all to miners
    w.blocks[publish_pointer(w.chain, env, E1 + 5)] = env
    w.chain.advance_to(E1 + 20)
    out = slave_consume(w.sub, FakeMetagraph(), FakeWallet(SLAVE), w.cfg(role="slave"))
    assert out.status == "applied", out
    applied = [row for _, uid, row in w.chain.applied_weight_sets if uid == HOTKEYS.index(SLAVE.ss58_address)]
    assert applied and dict(applied[-1]).keys() == set(b.uids)


# ------------------------------------------------------------------ single setter

def test_other_active_setter_blocks_weights_mode():
    from setter_guard import SetterGuardError, assert_single_setter, other_active_setters
    mg = SimpleNamespace(hotkeys=["me", "other-validator", "old", "miner"], validator_permit=[True, True, True, False],
                         last_update=[0, 9_900, 5_000, 9_990])
    assert other_active_setters(mg, "me", 10_000, 1080) == ["other-validator"]
    with pytest.raises(SetterGuardError):
        assert_single_setter(mg, "me", 10_000)
    assert_single_setter(SimpleNamespace(hotkeys=["me", "old"], validator_permit=[True, True],
                                         last_update=[0, 100]), "me", 10_000)
    with pytest.raises(SetterGuardError):
        assert_single_setter(SimpleNamespace(hotkeys=["me"]), "me", 1)


def test_process_lock_excludes_second_process(tmp_path):
    from setter_guard import acquire_process_lock, release_process_lock
    acquire_process_lock(str(tmp_path))
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from setter_guard import acquire_process_lock, SetterGuardError
        try:
            acquire_process_lock({str(tmp_path)!r}); print("ACQUIRED")
        except SetterGuardError:
            print("REFUSED")
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "REFUSED", out.stderr
    release_process_lock(str(tmp_path))


def test_startup_guards_always_run_before_the_loop(tmp_path, monkeypatch):
    import main
    from setter_guard import SetterGuardError, release_process_lock
    env = _env(netuid=360, bundle_state_dir=str(tmp_path), allocation_expected_genesis_hash=GENESIS,
               master_validator_hotkey_ss58=MASTER.ss58_address)
    sub = FakeSubtensor(FakeChain(head=10_000))
    query = sub.substrate.query
    def with_permits(module, storage_function, params, block_hash=None):
        if storage_function == "ValidatorPermit":
            return SimpleNamespace(value=[True] * len(HOTKEYS))
        return query(module, storage_function, params, block_hash)
    monkeypatch.setattr(sub.substrate, "query", with_permits)
    wallet = FakeWallet(MASTER)
    busy = SimpleNamespace(hotkeys=["me", "x"], validator_permit=[True, True], last_update=[0, 9_999])
    with pytest.raises(SetterGuardError):
        main.enforce_v3_startup_guards(sub, busy, FakeWallet(SLAVE), env)   # wrong signer
    main.enforce_v3_startup_guards(sub, busy, wallet, env)
    release_process_lock(str(tmp_path))
    # No configuration skips them: a default slave reusing the master hotkey is refused.
    with pytest.raises(SetterGuardError, match="distinct hotkey"):
        main.enforce_v3_startup_guards(sub, busy, FakeWallet(MASTER),
                                      _env(bundle_role="slave", bundle_ipfs_api_url="",
                                           bundle_gateway_urls="https://gateway.example.invalid",
                                           bundle_state_dir=str(tmp_path / "slave"),
                                           allocation_expected_genesis_hash=GENESIS,
                                           master_validator_hotkey_ss58=MASTER.ss58_address))


# ------------------------------------------------------------------ cross-repo contract

GO_FIXTURE = Path(__file__).parent / "fixtures" / "recall_v3_metrics_from_web_go.json"


def test_go_generated_evidence_parses_and_allocates():
    """recall-web's Go encoder output (TestBuildV3Miners_Contract) parses strictly here and
    feeds the allocator; units + burn conserve the 100-unit budget."""
    body = json.loads(GO_FIXTURE.read_text())["body"]
    cohort = {"hk-healthy": 1, "hk-db-only": 2, "hk-none": 3}
    ev = parse_v3_metrics(body, 353, 7200, cohort)
    assert ev.demand["vdb_query"] == 12.0 and len(ev.miners) == 3
    snap = [(1, "hk-healthy", "ck1", 10), (2, "hk-db-only", "ck2", 11), (3, "hk-none", "ck3", 12)]
    miners = to_miner_evidence(ev, snap)
    r = allocate_v3(miners, ev.demand, None, 7200, redistribute=False)
    assert abs(sum(r.units_by_uid.values()) + r.unassigned_units - 100.0) < 1e-9
    assert 3 not in r.units_by_uid or r.units_by_uid[3] == 0
