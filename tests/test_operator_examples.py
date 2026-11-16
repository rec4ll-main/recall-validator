"""Operator templates ship the mainnet production profile and keep secondary credentials separate.

These are configuration-only tests; they never instantiate wallets or RPC clients.
"""
from pathlib import Path
import os

import pytest
from dotenv import dotenv_values

from environment import (
    EnvironmentVariables, FINNEY_GENESIS_HASH, MAINNET_MASTER_HOTKEY_SS58, REMOVED_SETTINGS,
)
from main import validate_deployment_secrets

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch):
    # Process variables override .env by design; test the shipped templates alone.
    fields = set(EnvironmentVariables.model_fields)
    for key in tuple(os.environ):
        if key.lower() in fields:
            monkeypatch.delenv(key)


def values(name):
    return dotenv_values(ROOT / name)


@pytest.mark.parametrize("name,role", [(".env.example", "slave"), ("config/master.env.example", "master")])
def test_example_roles_are_explicit_and_carry_no_removed_settings(name, role):
    data = values(name)
    assert data["NETWORK"] == "finney"
    assert data["NETUID"] == "31"
    assert data["BUNDLE_ROLE"] == role
    assert data["BUNDLE_MODE"] == "chain-v2"
    assert data["ALLOCATION_MODE"] == "v2"
    for removed in REMOVED_SETTINGS:
        assert removed.upper() not in data, removed
    assert data["ALLOCATION_EXPECTED_GENESIS_HASH"] == FINNEY_GENESIS_HASH
    assert data["MASTER_VALIDATOR_HOTKEY_SS58"] == MAINNET_MASTER_HOTKEY_SS58
    # The Recall IPFS endpoints are plain HTTP (bundle data is public,
    # and every bundle is CID- and signature-verified).
    if role == "slave":
        assert data["BUNDLE_IPFS_API_URL"] == ""
        assert data["BUNDLE_GATEWAY_URLS"] == "http://ipfs.rec4ll.com:8080"
    else:
        assert data["BUNDLE_IPFS_API_URL"] == "http://ipfs.rec4ll.com:5001"
    assert data["IPFS_ALLOW_PLAINTEXT"] == "true"
    unknown = {key.lower() for key in data} - set(EnvironmentVariables.model_fields)
    assert not unknown, f"Example contains unknown settings: {unknown}"


def test_secondary_example_needs_no_recall_api_key():
    data = values(".env.example")
    assert "INTERNAL_API_KEY" not in data
    assert "RECALL_API_URL" not in data
    env = EnvironmentVariables(
        _env_file=ROOT / ".env.example",
        bundle_gateway_urls="https://gateway.example.invalid",
    )
    validate_deployment_secrets(env)
    assert env.bundle_role == "slave"
    assert env.network == "finney" and env.netuid == 31


@pytest.mark.parametrize("name", [".env.example", "config/master.env.example"])
def test_templates_load_the_mainnet_production_profile_without_any_flag(name):
    env = EnvironmentVariables(_env_file=ROOT / name)
    assert (env.network, env.netuid) == ("finney", 31)
    assert env.allocation_expected_genesis_hash == FINNEY_GENESIS_HASH
    assert env.master_validator_hotkey_ss58 == MAINNET_MASTER_HOTKEY_SS58
    assert env.incentive_v3_slot_window_hours == 48
    assert all(getattr(env, removed) is None for removed in REMOVED_SETTINGS)
    # Any edit away from the production profile is still refused on mainnet.
    with pytest.raises(ValueError, match="official Recall master"):
        EnvironmentVariables(_env_file=ROOT / name, master_validator_hotkey_ss58="5Other")


def test_master_example_requires_privately_provisioned_key():
    assert values("config/master.env.example")["INTERNAL_API_KEY"] == ""
    env = EnvironmentVariables(
        _env_file=ROOT / "config/master.env.example",
        bundle_ipfs_api_url="https://ipfs.example.invalid",
    )
    with pytest.raises(RuntimeError, match="INTERNAL_API_KEY must be a real secret"):
        validate_deployment_secrets(env)


def test_systemd_template_runs_the_locked_entry_point_with_narrow_writes():
    text = (ROOT / "deploy/systemd/recall-validator.service.example").read_text()
    directives = {}
    for line in text.splitlines():
        if line and not line.startswith(("#", "[")) and "=" in line:
            key, value = line.split("=", 1)
            directives.setdefault(key, []).append(value)
    assert directives["ExecStart"] == ["@INSTALL_DIR@/.venv/bin/python @INSTALL_DIR@/main.py"]
    assert directives["WorkingDirectory"] == ["@INSTALL_DIR@"]
    assert directives["EnvironmentFile"] == ["@ENV_FILE@"]
    assert directives["User"] == ["@SERVICE_USER@"]
    assert directives["ReadWritePaths"] == ["@STATE_DIR@"]
    assert directives["ProtectSystem"] == ["strict"]
    assert directives["NoNewPrivileges"] == ["true"]
    assert directives["ProtectHome"] == ["read-only"]
    assert "READ_ONLY=1" in directives["Environment"]
    assert "uv run" not in text and "pip install" not in text
    # main.py must stay the runtime entry point the template names.
    assert (ROOT / "main.py").read_text().rstrip().endswith('if __name__ == "__main__":\n    main()')


@pytest.mark.parametrize("name", [".env.example", "config/master.env.example"])
def test_templates_keep_unlimited_stale_reuse(name):
    """A max stale age changes payouts during publication outages; it is opt-in only."""
    assert values(name).get("WEIGHT_MAX_STALE_BLOCKS") in (None, "0")
    assert EnvironmentVariables.model_fields["weight_max_stale_blocks"].default == 0
