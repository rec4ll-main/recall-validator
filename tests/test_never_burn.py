"""Recall never burns miner emissions (equal split instead of the owner UID)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from equal_split import select_miners, snapshot_miners

HK = ["5owner", "5val", "5miner2", "5permitNoTrust", "5miner4"]
PERMIT = [True, True, False, True, False]
VTRUST = [0.9, 0.8, 0.0, 0.0, 0.0]


def test_miners_exclude_owner_and_active_validators_only():
    got = select_miners(HK, PERMIT, VTRUST, exclude_hotkeys=("5owner",))
    assert got == [(2, "5miner2"), (3, "5permitNoTrust"), (4, "5miner4")]


def test_miners_without_validator_arrays_exclude_only_given_hotkeys():
    assert select_miners(HK, None, None, exclude_hotkeys=("5owner", None)) == [
        (1, "5val"), (2, "5miner2"), (3, "5permitNoTrust"), (4, "5miner4")]


def test_snapshot_miners_ignore_mismatched_metagraph_arrays():
    mg = SimpleNamespace(validator_permit=PERMIT[:3], Tv=VTRUST[:3])  # wrong length: not trusted
    assert [u for u, _ in snapshot_miners(HK, mg, "5owner")] == [1, 2, 3, 4]
    mg = SimpleNamespace(validator_permit=PERMIT, Tv=VTRUST)
    assert [u for u, _ in snapshot_miners(HK, mg, "5owner", exclude_hotkeys=("5miner4",))] == [2, 3]


def _env(**kw):
    from environment import EnvironmentVariables
    base = dict(bundle_mode="chain-v2", bundle_role="master", allocation_mode="v2",
                allocation_expected_genesis_hash="0xgenesis", master_validator_hotkey_ss58="5Master",
                bundle_ipfs_api_url="http://127.0.0.1:5001", network="test", netuid=353, _env_file=None)
    base.update(kw)
    return EnvironmentVariables(**base)


def test_burn_settings_are_removed():
    import dataclasses

    from bundle_runtime import BundleConfig
    assert not [f.name for f in dataclasses.fields(BundleConfig) if "burn" in f.name]


def test_old_burn_settings_are_ignored_not_fatal(caplog):
    # A validator whose config still names the removed burn switches keeps running.
    with caplog.at_level(logging.WARNING):
        _env(incentive_v3_burn_unearned="true", weight_failclosed_policy="burn")
    assert "INCENTIVE_V3_BURN_UNEARNED is removed and ignored" in caplog.text
    assert "WEIGHT_FAILCLOSED_POLICY is removed and ignored" in caplog.text


def test_chain_miners_owner_read_failure_raises_never_includes_owner():
    import pytest
    from equal_split import chain_miners

    class Sub:
        def metagraph(self, netuid, lite=True):
            return SimpleNamespace(hotkeys=HK, validator_permit=PERMIT, Tv=VTRUST)

        class substrate:
            @staticmethod
            def query(*a, **k):
                raise ConnectionError("rpc down")

    with pytest.raises(ConnectionError):
        chain_miners(Sub(), 31)
