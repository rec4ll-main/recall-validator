import asyncio
import json
import time

from environment import EnvironmentVariables
from main import (
    api_endpoint_url,
    epistula_headers,
    publish_metagraph_sync,
    resolve_burn_uid,
)


class FakeSubnetInfo:
    def __init__(self, owner_ss58="", owner_hotkey=""):
        self.owner_ss58 = owner_ss58
        self.owner_hotkey = owner_hotkey


class FakeSubtensor:
    def __init__(self):
        self.calls = []
        self._subnet_info = None

    def set_weights(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(success=True)

    def get_subnet_info(self, netuid):
        if self._subnet_info is not None:
            return self._subnet_info
        raise AttributeError("no subnet info")


class FakeResponse:
    def __init__(self, success, message=""):
        self.success = success
        self.message = message


class FakeMetagraph:
    def __init__(self, hotkeys, coldkeys=None):
        self.hotkeys = hotkeys
        self.coldkeys = coldkeys
        self.sync_calls = []

    def sync(self, **kwargs):
        self.sync_calls.append(kwargs)


class FakeHTTPResponse:
    def __init__(self, body, status=200, headers=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, amt=None):
        # Match urllib response.read(amt) used by S-16 body cap.
        if amt is None or amt < 0:
            return self.body
        return self.body[:amt]


class FakeHotkey:
    ss58_address = "validator-hotkey"

    def __init__(self):
        self.messages = []

    def sign(self, message):
        self.messages.append(message)
        return b"\x11" * 64


class FakeWallet:
    def __init__(self):
        self.hotkey = FakeHotkey()


def _chain_v2_env(**overrides):
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


def test_bundle_mode_rejects_legacy_weight_fetch():
    try:
        EnvironmentVariables(bundle_mode="", allocation_mode="legacy", _env_file=None)
    except ValueError as exc:
        assert "chain-v2" in str(exc)
        return
    raise AssertionError("empty BUNDLE_MODE must be rejected")


def test_environment_variables_default_recall_api_url():
    env = _chain_v2_env()

    assert env.recall_api_url == "https://rec4ll.com/api"


def test_api_endpoint_url_handles_base_with_api_path():
    assert (
        api_endpoint_url("https://rec4ll.com/api", "/api/validator/weights")
        == "https://rec4ll.com/api/validator/weights"
    )
    assert (
        api_endpoint_url("https://rec4ll.com/api/", "/api/validator/metagraph-sync")
        == "https://rec4ll.com/api/validator/metagraph-sync"
    )
    assert (
        api_endpoint_url("http://api:8000", "/api/validator/weights")
        == "http://api:8000/api/validator/weights"
    )


def test_epistula_headers_sign_body_hash_uuid_timestamp_and_receiver():
    wallet = FakeWallet()

    headers = epistula_headers(wallet, "receiver-hotkey", b'{"ok":true}')

    assert headers["Epistula-Signed-By"] == "validator-hotkey"
    assert headers["Epistula-Signed-For"] == "receiver-hotkey"
    assert headers["Epistula-Request-Signature"] == "0x" + "11" * 64
    assert headers["Epistula-Uuid"]
    assert headers["Epistula-Timestamp"]
    signed_message = wallet.hotkey.messages[0].decode("utf-8")
    assert headers["Epistula-Uuid"] in signed_message
    assert headers["Epistula-Timestamp"] in signed_message
    assert signed_message.endswith(".receiver-hotkey")


def test_publish_metagraph_sync_posts_active_miners():
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data.decode("utf-8")
        return FakeHTTPResponse('{"status":"ok"}')

    ok = publish_metagraph_sync(
        "https://api.example.invalid/",
        "internal-secret",
        [(2, "miner-a"), (4, "miner-b")],
        timeout=7,
        urlopen=fake_urlopen,
    )

    assert ok is True
    assert captured["url"] == "https://api.example.invalid/api/validator/metagraph-sync"
    assert captured["timeout"] == 7
    assert captured["headers"]["X-internal-api-key"] == "internal-secret"
    assert '"hotkey": "miner-a"' in captured["body"]


def test_validate_deployment_secrets_armed_when_environment_empty():
    """An unset ENVIRONMENT must arm the guard (B1 fail-closed contract):
    only explicit dev/test are exempt."""
    from main import validate_deployment_secrets

    class FakeEnv:
        environment = ""
        internal_api_key = "local-api-internal-key"

    try:
        validate_deployment_secrets(FakeEnv())
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError for empty environment with placeholder key")


def test_validate_deployment_secrets_chain_v2_slave_needs_no_api_key():
    """Slave consumes the published bundle and does not call the web API."""
    from main import validate_deployment_secrets

    class FakeEnv:
        environment = "dev"
        internal_api_key = "local-api-internal-key"
        bundle_mode = "chain-v2"
        bundle_role = "slave"
        empty_organic_burn_enabled = False
        model_fields_set = frozenset({"netuid", "network"})

    validate_deployment_secrets(FakeEnv())


def test_validate_deployment_secrets_rejects_non_chain_v2_even_in_test():
    from main import validate_deployment_secrets

    class FakeEnv:
        environment = "test"
        internal_api_key = "local-api-internal-key"
        bundle_mode = ""
        bundle_role = ""
        empty_organic_burn_enabled = False
        model_fields_set = frozenset({"netuid", "network"})

    try:
        validate_deployment_secrets(FakeEnv())
    except RuntimeError as exc:
        assert "chain-v2" in str(exc)
        return
    raise AssertionError("non chain-v2 mode must be rejected")


def test_resolve_burn_uid_owner_hotkey_match():
    subtensor = FakeSubtensor()
    subtensor._subnet_info = FakeSubnetInfo(owner_hotkey="owner-hotkey")
    metagraph = FakeMetagraph(["validator", "miner-a", "owner-hotkey"])
    assert resolve_burn_uid(subtensor, metagraph, 7) == 2


def test_resolve_burn_uid_owner_coldkey_via_metagraph_coldkeys():
    subtensor = FakeSubtensor()
    subtensor._subnet_info = FakeSubnetInfo(owner_ss58="owner-coldkey")
    metagraph = FakeMetagraph(
        ["validator", "miner-a", "owner-hotkey"],
        coldkeys=["validator-coldkey", "miner-a-coldkey", "owner-coldkey"],
    )
    assert resolve_burn_uid(subtensor, metagraph, 7) == 2


def test_resolve_burn_uid_owner_ss58_matches_hotkey_legacy():
    subtensor = FakeSubtensor()
    subtensor._subnet_info = FakeSubnetInfo(owner_ss58="miner-a")
    metagraph = FakeMetagraph(["validator", "miner-a"], coldkeys=["validator-coldkey", "miner-a-coldkey"])
    assert resolve_burn_uid(subtensor, metagraph, 7) == 1


def test_resolve_burn_uid_no_match_returns_none():
    subtensor = FakeSubtensor()
    subtensor._subnet_info = FakeSubnetInfo(owner_ss58="someone-else")
    metagraph = FakeMetagraph(
        ["validator", "miner-a"],
        coldkeys=["validator-coldkey", "miner-a-coldkey"],
    )
    assert resolve_burn_uid(subtensor, metagraph, 7) is None


def test_resolve_burn_uid_get_subnet_info_error_returns_none():
    subtensor = FakeSubtensor()
    metagraph = FakeMetagraph(["validator", "miner-a"])
    assert resolve_burn_uid(subtensor, metagraph, 7) is None


def test_publish_metagraph_sync_refuses_unqualified_empty():
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        return FakeHTTPResponse('{"status":"ok"}')

    ok = publish_metagraph_sync(
        "https://api.example.invalid/", "internal-secret", [], urlopen=fake_urlopen
    )
    assert ok is False
    assert calls == []


def test_publish_metagraph_sync_sends_authoritative_empty_metadata():
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = request.data.decode("utf-8")
        return FakeHTTPResponse('{"status":"ok"}')

    ok = publish_metagraph_sync(
        "https://api.example.invalid/",
        "internal-secret",
        [],
        netuid=353,
        metagraph_block=999,
        authoritative_empty=True,
        urlopen=fake_urlopen,
    )
    assert ok is True
    payload = json.loads(captured["body"])
    assert payload["authoritative_empty"] is True
    assert payload["netuid"] == 353
    assert payload["metagraph_block"] == 999
    assert payload["miners"] == []


def test_publish_metagraph_sync_non_empty_includes_metadata():
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = request.data.decode("utf-8")
        return FakeHTTPResponse('{"status":"ok"}')

    ok = publish_metagraph_sync(
        "https://api.example.invalid/",
        "internal-secret",
        [(2, "miner-a")],
        netuid=353,
        metagraph_block=999,
        authoritative_empty=False,
        urlopen=fake_urlopen,
    )
    assert ok is True
    payload = json.loads(captured["body"])
    assert payload["authoritative_empty"] is False
    assert payload["netuid"] == 353
    assert payload["miners"] == [{"uid": 2, "hotkey": "miner-a"}]


def test_weights_with_burn_remainder_sorts_ascending():
    """F1: legacy remainder must not leave burn UID trailing after higher survivors."""
    from main import weights_with_burn_remainder

    uids, weights = weights_with_burn_remainder([(5, 0.1), (12, 0.1)], burn_uid=0)
    assert uids == [0, 5, 12]
    assert abs(sum(weights) - 1.0) < 1e-9
    assert abs(weights[0] - 0.8) < 1e-9


def test_validate_deployment_secrets_armed_requires_explicit_netuid():
    """F7: armed posture refuses silent NETUID/NETWORK defaults."""
    from main import validate_deployment_secrets

    class FakeEnv:
        environment = ""
        internal_api_key = "real-not-placeholder-key-value"
        master_validator_hotkey_ss58 = "5MasterHotkey"
        empty_organic_burn_enabled = False
        bundle_mode = "chain-v2"
        bundle_role = "slave"
        model_fields_set = frozenset()  # netuid/network unset

    try:
        validate_deployment_secrets(FakeEnv())
    except RuntimeError as exc:
        assert "NETUID" in str(exc)
        return
    raise AssertionError("expected RuntimeError for missing explicit NETUID")
