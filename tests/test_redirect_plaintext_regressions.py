"""Regression tests: S-15 redirect and S-14 plaintext split.

The S-4/S-13/S-5 empty-organic burn guards were removed with the score_version 10
allocator they tested.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

# ---------------------------------------------------------------------------
# S-15 — API requests never follow redirects
# ---------------------------------------------------------------------------






def test_s15_no_redirect_handler_raises_on_redirect_request():
    from main import _NoRedirect

    handler = _NoRedirect()
    req = urllib.request.Request("https://origin.example/api", data=b"{}", method="POST")
    req.add_header("X-Internal-API-Key", "secret")
    with pytest.raises(urllib.error.HTTPError) as ei:
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/")
    assert ei.value.code == 302
    assert "redirect not allowed" in str(ei.value.reason).lower() or "redirect not allowed" in str(
        ei.value
    )


# ---------------------------------------------------------------------------
# S-16 — body read capped at 1 MiB
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# S-14 — plaintext flags split (IPFS vs score)
# ---------------------------------------------------------------------------



def _chain_v2_env(**overrides):
    from environment import EnvironmentVariables

    base = dict(
        bundle_mode="chain-v2",
        bundle_role="master",
        allocation_mode="v2",
        allocation_expected_genesis_hash="0xgenesis",
        network="test",
        master_validator_hotkey_ss58="5MasterHotkey",
        bundle_ipfs_api_url="http://127.0.0.1:5001",
        _env_file=None,
    )
    base.update(overrides)
    return EnvironmentVariables(**base)


def test_s14_env_flags_independent_defaults():
    """OBSERVED: score plaintext stays false; IPFS plaintext defaults true."""
    from environment import EnvironmentVariables

    env = _chain_v2_env()
    assert env.allocation_allow_plaintext is False
    assert env.ipfs_allow_plaintext is True


def test_s14_env_ipfs_explicit_false_still_honored():
    """Stricter deploys can still set IPFS_ALLOW_PLAINTEXT=false."""
    from environment import EnvironmentVariables

    env = _chain_v2_env(ipfs_allow_plaintext=False)
    assert env.ipfs_allow_plaintext is False
    assert env.allocation_allow_plaintext is False


def test_s14_bundle_config_ipfs_default_true_score_stays_false():
    from bundle_runtime import BundleConfig

    cfg = BundleConfig(
        netuid=7,
        genesis_hash="0x0",
        master_hotkey="hk",
        state_dir="/tmp",
        role="master",
    )
    assert cfg.allow_plaintext is False
    assert cfg.ipfs_allow_plaintext is True


def test_s14_bundle_config_ipfs_flag_independent_of_score_flag():
    from bundle_runtime import BundleConfig

    cfg = BundleConfig(
        netuid=7,
        genesis_hash="0x0",
        master_hotkey="hk",
        state_dir="/tmp",
        role="master",
        allow_plaintext=False,
        ipfs_allow_plaintext=True,
    )
    assert cfg.allow_plaintext is False
    assert cfg.ipfs_allow_plaintext is True


def test_s14_publish_uses_ipfs_allow_plaintext_not_score_flag():
    """OBSERVED coupling fix: publish_block must see cfg.ipfs_allow_plaintext."""
    import bundle_publish

    src = open(bundle_publish.__file__).read()
    assert "allow_plaintext=cfg.ipfs_allow_plaintext" in src
    assert "allow_plaintext=cfg.allow_plaintext" not in src.replace(
        "allow_plaintext=cfg.ipfs_allow_plaintext", ""
    )


def test_s14_envelope_score_fetch_still_uses_allow_plaintext():
    """Evidence path (API key) keeps cfg.allow_plaintext — not the IPFS flag."""
    import v3_runtime

    src = open(v3_runtime.__file__).read()
    assert 'allow_plaintext=cfg.allow_plaintext' in src
    assert "ipfs_allow_plaintext" not in src


def test_s15_v3_metrics_redirect_refused():
    from validator_http import ApiFetchError, NO_REDIRECT_OPENER, _NoRedirect

    assert any(isinstance(h, _NoRedirect) for h in NO_REDIRECT_OPENER.handlers)
    req = urllib.request.Request("https://origin.example/api", data=b"{}", method="POST")
    with pytest.raises(ApiFetchError, match="redirect not allowed"):
        _NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://evil.example/")


# ---------------------------------------------------------------------------
# S-14a — IPFS plaintext host scope (not global arbitrary public)
# ---------------------------------------------------------------------------


def test_s14a_default_env_hosts_include_ipfs_read_url_host():
    """OBSERVED default IPFS_READ_URL host is in the plaintext allowlist."""
    from environment import EnvironmentVariables

    env = _chain_v2_env()
    assert env.ipfs_allow_plaintext is True
    hosts = env.ipfs_plaintext_allowed_hosts()
    assert set(hosts) >= {"ipfs.rec4ll.com"}
    assert "example.com" not in hosts


def test_s14a_bundle_ipfs_api_url_and_extra_hosts_merge():
    from environment import EnvironmentVariables, configured_ipfs_plaintext_hosts

    env = _chain_v2_env(
        bundle_ipfs_api_url="http://ipfs.rec4ll.com:5001",
        ipfs_plaintext_hosts="kubo.local,https://extra.ipfs.example:5001",
    )
    hosts = env.ipfs_plaintext_allowed_hosts()
    assert set(hosts) >= {"ipfs.rec4ll.com", "kubo.local", "extra.ipfs.example"}

    only = configured_ipfs_plaintext_hosts(
        bundle_ipfs_api_url="http://only.configured:5001",
        ipfs_read_url="",
    )
    assert only == ("only.configured",)


def test_s14a_main_wires_plaintext_hosts_into_bundle_config():
    """OBSERVED: run_bundle_epoch BundleConfig gets env-derived host allowlist."""
    import main as main_mod

    src = open(main_mod.__file__).read()
    assert "ipfs_plaintext_hosts=env.ipfs_plaintext_allowed_hosts()" in src


def test_s14a_publish_consume_pass_plaintext_hosts():
    import bundle_publish
    import bundle_consume

    pub = open(bundle_publish.__file__).read()
    con = open(bundle_consume.__file__).read()
    assert "plaintext_hosts=cfg.ipfs_plaintext_hosts" in pub
    assert "plaintext_hosts=cfg.ipfs_plaintext_hosts" in con
