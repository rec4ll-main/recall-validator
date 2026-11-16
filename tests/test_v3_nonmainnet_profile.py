"""Configurations that are not mainnet (offline fixtures, a pinned local chain).

Mainnet names and the Finney pin always route to the mainnet production profile; a
pinned non-mainnet chain still has its actual RPC genesis proved at runtime.
"""

import pytest

from environment import EnvironmentVariables, FINNEY_GENESIS_HASH


TEST_GENESIS = "0x" + "ab" * 32
OTHER_GENESIS = "0x" + "cd" * 32


def settings(**overrides):
    values = dict(
        bundle_mode="chain-v2", bundle_role="slave", allocation_mode="v2",
        allocation_expected_genesis_hash=TEST_GENESIS,
        master_validator_hotkey_ss58="test-master", bundle_state_dir="test-state",
        bundle_gateway_urls="https://gateway.example.invalid",
        network="wss://rpc.example.invalid", netuid=9999,
        _env_file=None,
    )
    if overrides.get("bundle_role") == "master":
        values["bundle_ipfs_api_url"] = "https://ipfs.example.invalid"
    values.update(overrides)
    return EnvironmentVariables(**values)


def test_pinned_non_mainnet_chain_loads_with_canonical_genesis():
    env = settings(allocation_expected_genesis_hash=TEST_GENESIS.upper())
    assert env.netuid == 9999
    assert env.allocation_expected_genesis_hash == TEST_GENESIS


@pytest.mark.parametrize("network", ["finney", "mainnet", "archive",
    "wss://finney.example.invalid", "wss://mainnet.example.invalid"])
def test_mainnet_names_and_url_aliases_get_the_mainnet_profile(network):
    with pytest.raises(ValueError, match="mainnet"):
        settings(network=network)


@pytest.mark.parametrize("network", ["test", "wss://rpc-alias.example.invalid"])
def test_mainnet_genesis_is_forbidden_behind_any_alias(network):
    with pytest.raises(ValueError, match="mainnet"):
        settings(network=network, allocation_expected_genesis_hash=FINNEY_GENESIS_HASH)


def test_runtime_still_refuses_actual_genesis_mismatch_before_consume(tmp_path):
    import main
    from bundle_runtime import BundleRuntimeError
    from chain_v2_fake import FakeChain, FakeMetagraph, FakeSubtensor, FakeWallet, SLAVE

    chain = FakeChain(head=3620)
    sub = FakeSubtensor(chain)
    env = settings(allocation_expected_genesis_hash=OTHER_GENESIS, bundle_state_dir=str(tmp_path))
    with pytest.raises(BundleRuntimeError, match="genesis hash mismatch"):
        main.run_bundle_epoch(sub, FakeWallet(SLAVE), FakeMetagraph(), env)
    assert not chain.submissions
