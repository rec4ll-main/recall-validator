"""Startup refusal of settings that would otherwise fail silently at runtime."""

import logging

import pytest

from environment import EnvironmentVariables, FINNEY_GENESIS_HASH, MAINNET_MASTER_HOTKEY_SS58

TEST_GENESIS = "0x" + "ab" * 32


def settings(**overrides):
    values = dict(
        bundle_mode="chain-v2", bundle_role="slave", allocation_mode="v2",
        allocation_expected_genesis_hash=FINNEY_GENESIS_HASH,
        master_validator_hotkey_ss58=MAINNET_MASTER_HOTKEY_SS58, bundle_state_dir="test-state",
        bundle_gateway_urls="https://gateway.example.invalid",
        network="finney", netuid=31, _env_file=None,
    )
    if overrides.get("bundle_role") == "master":
        values["bundle_ipfs_api_url"] = "https://ipfs.example.invalid"
        values["bundle_gateway_urls"] = ""
    values.update(overrides)
    return EnvironmentVariables(**values)


# --- plaintext IPFS URLs on mainnet ---------------------------------------------

@pytest.mark.parametrize("field,role", [
    ("bundle_gateway_urls", "slave"),
    ("bundle_ipfs_api_url", "master"),
])
def test_mainnet_plaintext_public_ipfs_url_refused_when_runtime_would_refuse(field, role):
    with pytest.raises(ValueError, match=field.upper() + " uses plaintext"):
        settings(bundle_role=role, ipfs_allow_plaintext=False,
                 **{field: "http://ipfs.example.invalid:8080"})


def test_mainnet_plaintext_refused_by_genesis_pin_even_with_network_alias():
    with pytest.raises(ValueError, match="uses plaintext"):
        settings(network="wss://rpc-alias.example.invalid", ipfs_allow_plaintext=False,
                 bundle_gateway_urls="https://gw.example.invalid,http://ipfs.example.invalid:8080")


def test_mainnet_plaintext_allowed_for_configured_hosts_when_plaintext_enabled():
    env = settings(bundle_gateway_urls="http://ipfs.example.invalid:8080", ipfs_allow_plaintext=True)
    assert env.bundle_gateway_urls == "http://ipfs.example.invalid:8080"
    master = settings(bundle_role="master", bundle_ipfs_api_url="http://ipfs.example.invalid:5001")
    assert master.bundle_ipfs_api_url.startswith("http://")


@pytest.mark.parametrize("url", ["http://127.0.0.1:5001", "http://localhost:5001"])
def test_mainnet_plaintext_loopback_master_api_allowed(url):
    env = settings(bundle_role="master", bundle_ipfs_api_url=url, ipfs_allow_plaintext=False)
    assert env.bundle_ipfs_api_url == url


def test_mainnet_https_ipfs_urls_allowed_without_plaintext():
    env = settings(ipfs_allow_plaintext=False)
    assert env.bundle_gateway_urls == "https://gateway.example.invalid"


# A pinned chain that is not mainnet: the startup plaintext check applies on every network.
OFFLINE = dict(network="wss://rpc.example.invalid", netuid=9999, allocation_expected_genesis_hash=TEST_GENESIS)


@pytest.mark.parametrize("field,role", [
    ("bundle_gateway_urls", "slave"),
    ("bundle_ipfs_api_url", "master"),
])
def test_offline_plaintext_public_ipfs_url_refused_when_runtime_would_refuse(field, role):
    with pytest.raises(ValueError, match=field.upper() + " uses plaintext"):
        settings(bundle_role=role, ipfs_allow_plaintext=False,
                 **{field: "http://ipfs.example.invalid:8080"}, **OFFLINE)


def test_offline_plaintext_allowed_where_runtime_allows_it():
    env = settings(bundle_gateway_urls="http://ipfs.example.invalid:8080", ipfs_allow_plaintext=True,
                   **OFFLINE)
    assert env.bundle_gateway_urls == "http://ipfs.example.invalid:8080"
    loopback = settings(bundle_role="master", bundle_ipfs_api_url="http://127.0.0.1:5001",
                        ipfs_allow_plaintext=False, **OFFLINE)
    assert loopback.bundle_ipfs_api_url == "http://127.0.0.1:5001"
    https = settings(ipfs_allow_plaintext=False, **OFFLINE)
    assert https.bundle_gateway_urls == "https://gateway.example.invalid"


@pytest.mark.parametrize("profile", [
    dict(network="finney", netuid=31, allocation_expected_genesis_hash=FINNEY_GENESIS_HASH),
    OFFLINE,
], ids=["mainnet", "offline"])
@pytest.mark.parametrize("allow", [False, True])
def test_startup_plaintext_check_matches_runtime_transport(profile, allow):
    from bundle_ipfs import IpfsError, _validate_url

    gateway = "http://ipfs.example.invalid:8080"
    hosts = settings(bundle_gateway_urls=gateway, ipfs_allow_plaintext=True,
                     **profile).ipfs_plaintext_allowed_hosts()
    try:
        _validate_url(gateway, allow_plaintext=allow, plaintext_hosts=hosts)
        runtime_ok = True
    except IpfsError:
        runtime_ok = False
    try:
        settings(bundle_gateway_urls=gateway, ipfs_allow_plaintext=allow, **profile)
        started = True
    except ValueError as exc:
        assert "BUNDLE_GATEWAY_URLS uses plaintext" in str(exc)
        started = False
    assert runtime_ok is allow
    assert started is runtime_ok


# --- secondary IPFS API URL is ignored, never contacted ---------------------------

def test_secondary_ipfs_api_url_is_blanked_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="environment"):
        env = settings(bundle_ipfs_api_url="http://ipfs.example.invalid:5001")
    assert env.bundle_ipfs_api_url == ""
    assert "ipfs.example.invalid" not in env.ipfs_plaintext_allowed_hosts()
    assert any("BUNDLE_IPFS_API_URL is ignored on a secondary" in r.getMessage() for r in caplog.records)


def test_secondary_still_requires_gateway_after_blanking_api():
    with pytest.raises(ValueError, match="requires public BUNDLE_GATEWAY_URLS"):
        settings(bundle_ipfs_api_url="http://ipfs.example.invalid:5001", bundle_gateway_urls="")


def test_master_keeps_ipfs_api_url():
    assert settings(bundle_role="master").bundle_ipfs_api_url == "https://ipfs.example.invalid"


def test_mainnet_profile_still_requires_the_official_master():
    with pytest.raises(ValueError, match="official Recall master"):
        settings(master_validator_hotkey_ss58="test-master")
