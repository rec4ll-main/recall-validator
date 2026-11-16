"""Mainnet production profile: declared pins never substitute for RPC proof.

Incentive v3 needs no activation flag; a mainnet configuration (network alias or the
Finney pin) is held to the production profile instead, and startup still proves the
actual genesis, registered signer, permit and process lock.
"""

import copy
import json
from pathlib import Path

import pytest

import main
from environment import EnvironmentVariables, FINNEY_GENESIS_HASH, MAINNET_MASTER_HOTKEY_SS58
from setter_guard import SetterGuardError, release_process_lock
from test_v3_signer_startup import Chain, active_graph, wallet
from v3_metrics_client import V3MetricsError, parse_v3_metrics


def settings(**overrides):
    values = dict(
        bundle_mode="chain-v2", bundle_role="master", allocation_mode="v2",
        allocation_expected_genesis_hash=FINNEY_GENESIS_HASH,
        master_validator_hotkey_ss58=MAINNET_MASTER_HOTKEY_SS58,
        bundle_state_dir="test-state", bundle_ipfs_api_url="https://ipfs.example.invalid",
        network="finney", netuid=31, _env_file=None,
    )
    values.update(overrides)
    if values["bundle_role"] == "slave":
        values["bundle_ipfs_api_url"] = ""
        values["bundle_gateway_urls"] = "https://gateway.example.invalid"
    return EnvironmentVariables(**values)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv("INCENTIVE_V3_SLOT_WINDOW_HOURS", raising=False)


@pytest.mark.parametrize("network", ["finney", "mainnet", "wss://rpc.example.invalid"])
def test_canonical_mainnet_profile_is_accepted_without_any_activation_flag(network):
    env = settings(network=network)
    assert env.netuid == 31 and env.allocation_expected_genesis_hash == FINNEY_GENESIS_HASH


@pytest.mark.parametrize("value", ["", "0xnot-a-hash", "0x" + "ab" * 32, "0x" + "00" * 32])
def test_mainnet_network_requires_the_exact_finney_pin(value):
    with pytest.raises(ValueError):
        settings(allocation_expected_genesis_hash=value)


def test_mainnet_pins_are_canonicalized_for_actual_runtime_comparison():
    env = settings(allocation_expected_genesis_hash=FINNEY_GENESIS_HASH.upper())
    assert env.allocation_expected_genesis_hash == FINNEY_GENESIS_HASH


@pytest.mark.parametrize("overrides", [
    {"netuid": 9999},
    {"netuid": 32},
    {"incentive_v3_approved_testnet_genesis_hash": "0x" + "ab" * 32},
    {"network": "test"}, {"network": "testnet"}, {"network": "archive"},
    {"master_validator_hotkey_ss58": "different-master"},
    {"incentive_v3_generation_benchmark_policy_file": "policy.json"},
    {"settlement_checks_enabled": "true"},
    {"incentive_v3_eligibility_scope": "a" * 64},
    {"incentive_v3_slot_window_hours": 4},
])
def test_mainnet_profile_refuses_other_subnets_networks_and_test_settings(overrides):
    with pytest.raises(ValueError):
        settings(**overrides)


def test_actual_allocator_environment_cannot_hide_a_shortened_slot_window(monkeypatch):
    monkeypatch.setenv("INCENTIVE_V3_SLOT_WINDOW_HOURS", "4")
    with pytest.raises(ValueError, match="runtime requires.*48-hour"):
        settings(incentive_v3_slot_window_hours=48)


def mainnet_chain():
    chain = Chain()
    chain.genesis = FINNEY_GENESIS_HASH
    chain.uids = {MAINNET_MASTER_HOTKEY_SS58: 0, "secondary": 1}
    chain.keys = {0: MAINNET_MASTER_HOTKEY_SS58, 1: "secondary"}
    return chain


@pytest.mark.parametrize("role,signer", [("master", MAINNET_MASTER_HOTKEY_SS58),
                                         ("slave", "secondary")])
def test_actual_genesis_registered_signer_and_permit_are_required_at_startup(tmp_path, role, signer):
    env = settings(bundle_role=role, bundle_state_dir=str(tmp_path / role))
    chain = mainnet_chain()
    try:
        main.enforce_v3_startup_guards(chain, active_graph(), wallet(signer), env)
        assert chain.queries
        assert all(params[0] == 31 for _, params, _ in chain.queries)
    finally:
        release_process_lock(env.bundle_state_dir)


@pytest.mark.parametrize("change", [
    lambda c: setattr(c, "genesis", "0x" + "ab" * 32),
    lambda c: setattr(c, "fail", "head"),
    lambda c: setattr(c, "fail", "Uids"),
    lambda c: setattr(c, "fail", "Keys"),
    lambda c: setattr(c, "fail", "ValidatorPermit"),
    lambda c: c.permits.__setitem__(0, False),
    lambda c: c.keys.update({0: "different-master"}),
])
def test_mainnet_rpc_identity_and_finalized_proof_fail_closed(tmp_path, change):
    env = settings(bundle_state_dir=str(tmp_path))
    chain = mainnet_chain()
    change(chain)
    with pytest.raises(SetterGuardError):
        main.enforce_v3_startup_guards(chain, active_graph(), wallet(MAINNET_MASTER_HOTKEY_SS58), env)


def test_unavailable_genesis_refuses_mainnet_startup(tmp_path):
    chain = mainnet_chain()
    def unavailable(_):
        raise OSError("genesis unavailable")
    chain.get_block_hash = unavailable
    with pytest.raises(SetterGuardError):
        main.enforce_v3_startup_guards(chain, active_graph(), wallet(MAINNET_MASTER_HOTKEY_SS58),
                                      settings(bundle_state_dir=str(tmp_path)))


def test_mainnet_process_lock_is_required_at_startup(tmp_path, monkeypatch):
    def locked(_):
        raise SetterGuardError("already locked")
    monkeypatch.setattr("setter_guard.acquire_process_lock", locked)
    with pytest.raises(SetterGuardError, match="locked"):
        main.enforce_v3_startup_guards(mainnet_chain(), active_graph(), wallet(MAINNET_MASTER_HOTKEY_SS58),
                                      settings(bundle_state_dir=str(tmp_path)))


def test_ordinary_generation_evidence_remains_eligible_for_validation():
    body = json.loads((Path(__file__).parent / "fixtures/recall_v3_metrics_fixture.json").read_text())["body"]
    body["netuid"] = 31
    body["miners"][0]["tiers"]["gen"] = {
        "checks": [{"name": name, "n_eff": 100.0, "p_hat": 1.0}
                   for name in ("gen_qa", "gen_citation", "gen_abstention", "gen_injection")],
        "factor": 1.0, "h_passes": 100, "h_n": 100, "credited": {"gen": 1.0},
    }
    cohort = {m["hotkey"]: m["uid"] for m in body["miners"]}
    evidence = parse_v3_metrics(body, 31, body["epoch_start_block"], cohort)
    assert any("gen" in miner[3] for miner in evidence.miners)
    # Synthetic benchmark metadata is refused; ordinary generation evidence is not.
    injected = copy.deepcopy(body)
    injected["generation_benchmark"] = {"contract": "synthetic-testnet-benchmark-v1"}
    with pytest.raises(V3MetricsError, match="not accepted"):
        parse_v3_metrics(injected, 31, injected["epoch_start_block"], cohort)
