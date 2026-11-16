"""Incentive v3 (score_version 11) is the only scoring path; removed settings fail loudly.

INCENTIVE_V3_MODE=weights is accepted with a deprecation warning, off/shadow/anything
else refuses to start, and the other removed switches are ignored with a warning
(a contradicting removed genesis pin fails closed).
"""

from __future__ import annotations

import dataclasses
import inspect
import logging

import pytest

import bundle_envelope
import score_version
import v3_runtime
from bundle_runtime import BundleConfig
from environment import (
    FINNEY_GENESIS_HASH, MAINNET_MASTER_HOTKEY_SS58, REMOVED_SETTINGS, EnvironmentVariables,
)

TEST_GENESIS = "0x" + "ab" * 32


def _offline(**overrides):
    values = dict(
        bundle_mode="chain-v2", bundle_role="master", allocation_mode="v2",
        allocation_expected_genesis_hash=TEST_GENESIS, master_validator_hotkey_ss58="5Master",
        bundle_ipfs_api_url="http://127.0.0.1:5001", network="wss://rpc.example.invalid", netuid=9999,
        _env_file=None,
    )
    values.update(overrides)
    return EnvironmentVariables(**values)


def _mainnet(**overrides):
    values = dict(
        bundle_mode="chain-v2", bundle_role="slave", allocation_mode="v2",
        allocation_expected_genesis_hash=FINNEY_GENESIS_HASH,
        master_validator_hotkey_ss58=MAINNET_MASTER_HOTKEY_SS58,
        bundle_gateway_urls="https://gateway.example.invalid", network="finney", netuid=31,
        _env_file=None,
    )
    values.update(overrides)
    return EnvironmentVariables(**values)


@pytest.fixture(autouse=True)
def _no_process_overrides(monkeypatch):
    for name in REMOVED_SETTINGS + ("incentive_v3_slot_window_hours",):
        monkeypatch.delenv(name.upper(), raising=False)


# ------------------------------------------------------------------ v11 by default

def test_v11_is_active_by_default_with_no_setting():
    assert score_version.SUPPORTED_SCORE_VERSION == score_version.active_score_version() == "11"
    assert score_version.active_profile() == ("11", "readiness_service_v3", 21600)
    for env in (_offline(), _mainnet()):
        assert env.incentive_v3_mode is None
    assert "incentive_v3_mode" not in {f.name for f in dataclasses.fields(BundleConfig)}


def test_legacy_mode_machinery_is_gone():
    assert not hasattr(score_version, "activate_score_version_11")
    assert not hasattr(score_version, "reset_score_version_for_tests")
    assert not hasattr(v3_runtime, "run_v3_shadow")
    assert "mode" not in inspect.signature(v3_runtime.compute_v3_epoch).parameters
    assert v3_runtime.V3_STATE_MODE == "weights"   # on-disk namespace kept for existing state
    for module in ("tier_score_client", "allocation_submission", "allocation_mix", "organic_empty_burn"):
        with pytest.raises(ImportError):
            __import__(module)


def test_envelope_builder_always_uses_v3(monkeypatch):
    seen = []
    monkeypatch.setattr(v3_runtime, "build_envelope_v3",
                        lambda *a, **k: seen.append(k.get("fetch")) or b"v11")
    marker = object()
    assert bundle_envelope.build_envelope(None, None, None, None, None, fetch=marker) == b"v11"
    assert bundle_envelope.build_envelope(None, None, None, None, None) == b"v11"
    assert seen == [marker, None]


# ------------------------------------------------------------------ INCENTIVE_V3_MODE

@pytest.mark.parametrize("value", ["weights", " Weights "])
def test_mode_weights_is_accepted_with_a_deprecation_warning(caplog, value):
    with caplog.at_level(logging.WARNING, logger="environment"):
        env = _offline(incentive_v3_mode=value)
    assert env.incentive_v3_mode == value
    assert any("INCENTIVE_V3_MODE is deprecated" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["off", "shadow", "OFF", "on", "v10"])
def test_mode_off_shadow_or_unknown_refuses_to_start(value):
    with pytest.raises(ValueError, match="no longer supported.*Delete INCENTIVE_V3_MODE"):
        _offline(incentive_v3_mode=value)
    with pytest.raises(ValueError, match="no longer supported"):
        _mainnet(incentive_v3_mode=value)


def test_empty_mode_is_treated_as_unset(caplog):
    with caplog.at_level(logging.WARNING, logger="environment"):
        _offline(incentive_v3_mode="")
    assert not caplog.records


def test_mode_is_read_from_the_process_environment(monkeypatch):
    monkeypatch.setenv("INCENTIVE_V3_MODE", "shadow")
    with pytest.raises(ValueError, match="no longer supported"):
        _offline()


# ------------------------------------------------------------------ removed switches

@pytest.mark.parametrize("name", [n for n in REMOVED_SETTINGS if n not in (
    "incentive_v3_mode", "incentive_v3_approved_mainnet_genesis_hash")])
def test_removed_switch_is_ignored_with_a_warning(caplog, monkeypatch, name):
    monkeypatch.setenv(name.upper(), "true")
    with caplog.at_level(logging.WARNING, logger="environment"):
        env = _offline()
    assert getattr(env, name) == "true"
    assert any(f"{name.upper()} is removed and ignored" in r.getMessage() for r in caplog.records)


def test_old_mainnet_gate_no_longer_refuses_and_changes_nothing():
    # Before: INCENTIVE_V3_MODE=weights with the opt-in false was refused on mainnet.
    old = _mainnet(incentive_v3_mode="weights", incentive_v3_mainnet_31_opt_in="false",
                  incentive_v3_weights_netuids="31",
                  incentive_v3_approved_mainnet_genesis_hash=FINNEY_GENESIS_HASH)
    new = _mainnet()
    keep = set(EnvironmentVariables.model_fields) - set(REMOVED_SETTINGS)
    assert {k: getattr(old, k) for k in keep} == {k: getattr(new, k) for k in keep}


def test_removed_approved_mainnet_pin_must_not_contradict_the_expected_pin():
    assert _mainnet(incentive_v3_approved_mainnet_genesis_hash=FINNEY_GENESIS_HASH.upper())
    with pytest.raises(ValueError, match="contradicts ALLOCATION_EXPECTED_GENESIS_HASH"):
        _mainnet(incentive_v3_approved_mainnet_genesis_hash=TEST_GENESIS)
    with pytest.raises(ValueError, match="contradicts ALLOCATION_EXPECTED_GENESIS_HASH"):
        _offline(incentive_v3_approved_mainnet_genesis_hash=FINNEY_GENESIS_HASH)


def test_removed_v10_burn_settings_are_not_parsed():
    # Values the score_version 10 validator refused are now inert leftovers.
    env = _offline(organic_burn_residual="7", organic_burn_apply_order="anything")
    assert env.organic_burn_residual == "7"


# ------------------------------------------------------------------ safety kept

@pytest.mark.parametrize("overrides", [
    {"allocation_expected_genesis_hash": TEST_GENESIS},
    {"master_validator_hotkey_ss58": "5Other"},
    {"netuid": 9999},
    {"incentive_v3_slot_window_hours": 24},
    {"network": "archive"},
])
def test_mainnet_production_profile_still_enforced(overrides):
    with pytest.raises(ValueError, match="mainnet"):
        _mainnet(**overrides)


@pytest.mark.parametrize("name,value,match", [
    ("settlement_checks_enabled", "true", "SETTLEMENT_CHECKS_ENABLED is refused on mainnet"),
    ("incentive_v3_approved_testnet_genesis_hash", TEST_GENESIS, "testnet genesis"),
    ("incentive_v3_generation_benchmark_policy_file", "policy.json", "forbidden on mainnet"),
])
def test_removed_test_network_settings_are_still_refused_on_mainnet(name, value, match):
    with pytest.raises(ValueError, match=match):
        _mainnet(**{name: value})
    # Off mainnet the same leftover is only ignored.
    assert getattr(_offline(**{name: value}), name) == value


def test_removed_settlement_switch_left_false_on_mainnet_is_only_ignored(caplog):
    with caplog.at_level(logging.WARNING, logger="environment"):
        env = _mainnet(settlement_checks_enabled="false")
    assert env.netuid == 31
    assert any("SETTLEMENT_CHECKS_ENABLED is removed and ignored" in r.getMessage() for r in caplog.records)


def test_other_netuids_load_off_mainnet():
    assert _offline(netuid=354).netuid == 354
