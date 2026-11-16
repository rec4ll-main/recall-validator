"""Key-bearing API destinations: HTTPS for remote hosts, literal loopback only for
plaintext, no userinfo, no redirects, sanitized errors.

This covers a conditional source/configuration risk (a master configured with a
non-HTTPS API URL); it is not evidence of an observed leak. Every transport here is
injected or bound to a literal loopback port; nothing leaves the test host.
"""

import ast
import http.server
import inspect
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

import api_transport
import deployment_config
import main
from api_transport import ApiDestinationError, validate_api_url

ROOT = Path(__file__).resolve().parents[1]
SYNTH_KEY = "synthetic-internal-key-7f3a91"
SYNTH_USER = "synthuser4417"
SYNTH_PASS = "synthpass8823"

ACCEPTED = [
    "https://api.example.invalid/api",
    "https://api.example.invalid:8443/api/",
    "https://127.0.0.1/api",
    "http://127.0.0.1:8080/api",
    "http://127.0.0.2:8080",
    "http://[::1]:9000/api",
]

REJECTED = [
    "http://api.example.invalid/api",          # remote plaintext
    "HTTP://api.example.invalid/api",          # scheme case does not matter
    "http://localhost:8000/api",               # a name, not a literal loopback IP
    "http://127.1:8000/api",                   # non-canonical IP spelling
    "http://2130706433/api",
    "http://0x7f000001/api",
    "http://0.0.0.0:8000/api",
    "http://10.0.0.5:8000/api",                # private is still remote
    "ftp://api.example.invalid/api",
    "api.example.invalid/api",
    f"https://{SYNTH_USER}:{SYNTH_PASS}@api.example.invalid/api",
    f"https://{SYNTH_USER}@api.example.invalid/api",
    f"http://{SYNTH_USER}:{SYNTH_PASS}@127.0.0.1/api",
    f"https://api.example.invalid/api?token={SYNTH_PASS}",
    f"https://api.example.invalid/api#{SYNTH_PASS}",
    "https://api.example.invalid:notaport/api",
    "https://api.example.invalid:0/api",
    "https://api.example.invalid:70000/api",
    "https://:443/api",
    "https:///api",
    " https://api.example.invalid/api",
    "https://api.example.invalid/a pi",
    "https://api.example.invalid\\@127.0.0.1/api",
    "https://exa%mple.invalid/api",
    "",
]


@pytest.mark.parametrize("url", ACCEPTED)
def test_accepted_destinations(url):
    dest = validate_api_url(url)
    assert dest.plaintext_loopback == url.startswith("http://")


@pytest.mark.parametrize("url", REJECTED)
def test_rejected_destinations_do_not_echo_the_url(url):
    with pytest.raises(ApiDestinationError) as info:
        validate_api_url(url)
    message = str(info.value)
    for fragment in (SYNTH_USER, SYNTH_PASS, "example.invalid", "localhost", "notaport"):
        assert fragment not in message


def test_non_string_is_rejected():
    with pytest.raises(ApiDestinationError):
        validate_api_url(None)


def test_policy_has_no_plaintext_override():
    """ALLOCATION_ALLOW_PLAINTEXT cannot be threaded into the destination policy."""
    for fn in (api_transport.validate_api_url, api_transport.validated_endpoint, api_transport.send_request):
        params = inspect.signature(fn).parameters
        assert not any("plaintext" in name for name in params), fn.__name__


class Wallet:
    """Records any signature request; a refused destination must cause none."""

    def __init__(self):
        self.signed = []
        outer = self

        class Hotkey:
            ss58_address = "5SyntheticMasterHotkey"

            def sign(self, message):
                outer.signed.append(message)
                return b"\x00" * 64

        self.hotkey = Hotkey()


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *_):
        return b'{"status":"ok","active_miners":1,"marked_offline":0}'


@pytest.mark.parametrize("url", REJECTED)
@pytest.mark.parametrize("with_wallet", [False, True])
def test_rejected_destination_receives_zero_calls_and_no_key(url, with_wallet, caplog):
    calls = []
    wallet = Wallet() if with_wallet else None
    caplog.set_level(logging.DEBUG)
    ok = main.publish_metagraph_sync(
        url, SYNTH_KEY, [(3, "miner-hotkey")], netuid=353, metagraph_block=7200,
        wallet=wallet, urlopen=lambda request, timeout: calls.append(request) or Response(),
    )
    assert ok is False
    assert calls == []
    if wallet is not None:
        assert wallet.signed == []
    assert SYNTH_KEY not in caplog.text
    assert SYNTH_PASS not in caplog.text and SYNTH_USER not in caplog.text


@pytest.mark.parametrize("url", ["https://api.example.invalid/api", "http://127.0.0.1:8080/api"])
def test_accepted_destination_is_called_once(url):
    calls = []
    ok = main.publish_metagraph_sync(
        url, SYNTH_KEY, [(3, "miner-hotkey")], netuid=353, metagraph_block=7200,
        urlopen=lambda request, timeout: calls.append(request) or Response(),
    )
    assert ok is True and len(calls) == 1
    assert calls[0].full_url.endswith("/api/validator/metagraph-sync")


def test_destination_is_rechecked_immediately_before_the_post(monkeypatch, caplog):
    """Even if the first check were bypassed, the send-time check still refuses."""
    calls = []
    monkeypatch.setattr(main, "validated_endpoint", lambda base, path: "http://api.example.invalid/api/validator/metagraph-sync")
    ok = main.publish_metagraph_sync(
        "https://api.example.invalid/api", SYNTH_KEY, [(3, "miner-hotkey")],
        urlopen=lambda request, timeout: calls.append(request) or Response(),
    )
    assert ok is False and calls == []
    assert "must use https" in caplog.text and SYNTH_KEY not in caplog.text


def test_transport_errors_are_sanitized(caplog):
    def transport(request, timeout):
        raise urllib.error.URLError(OSError(f"connect to {SYNTH_PASS}.example.invalid failed key={SYNTH_KEY}"))

    assert not main.publish_metagraph_sync("https://api.example.invalid/api", SYNTH_KEY, [(3, "m")], urlopen=transport)
    assert "connection failed (OSError)" in caplog.text
    assert SYNTH_KEY not in caplog.text and SYNTH_PASS not in caplog.text


def test_describe_transport_error():
    err = urllib.error.HTTPError(f"https://{SYNTH_PASS}.example.invalid/x", 503, SYNTH_KEY, {}, None)
    assert api_transport.describe_transport_error(err) == "HTTP 503"
    assert api_transport.describe_transport_error(RuntimeError(SYNTH_KEY)) == "RuntimeError"


# --- startup -------------------------------------------------------------------- #

class MasterEnv:
    environment = ""
    bundle_mode = "chain-v2"
    bundle_role = "master"
    internal_api_key = SYNTH_KEY
    empty_organic_burn_enabled = False
    model_fields_set = frozenset({"netuid", "network", "recall_api_url"})

    def __init__(self, url, allow_plaintext=False):
        self.recall_api_url = url
        self.allocation_allow_plaintext = allow_plaintext
        self.log_level = "INFO"


@pytest.mark.parametrize("url", REJECTED[:-1])
@pytest.mark.parametrize("allow_plaintext", [False, True])
def test_startup_refuses_non_https_remote_master_url(url, allow_plaintext):
    with pytest.raises(RuntimeError) as info:
        deployment_config.validate_deployment_secrets(MasterEnv(url, allow_plaintext))
    message = str(info.value)
    assert "RECALL_API_URL refused" in message
    assert SYNTH_KEY not in message and SYNTH_PASS not in message and "example.invalid" not in message


@pytest.mark.parametrize("url", ACCEPTED)
def test_startup_accepts_https_and_literal_loopback(url):
    deployment_config.validate_deployment_secrets(MasterEnv(url))


def test_slave_startup_does_not_require_an_api_url():
    env = MasterEnv("http://api.example.invalid/api", allow_plaintext=True)
    env.bundle_role = "slave"
    deployment_config.validate_deployment_secrets(env)


def test_main_refuses_before_any_runtime(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("runtime entered")

    monkeypatch.setattr(main, "EnvironmentVariables", lambda: MasterEnv("http://api.example.invalid/api", True))
    monkeypatch.setattr(main, "run_validator", forbidden)
    with pytest.raises(RuntimeError, match="RECALL_API_URL refused"):
        main.main()


def test_main_reexports_the_shared_check():
    assert main.validate_deployment_secrets is deployment_config.validate_deployment_secrets


# --- real loopback transport: no redirect following, no proxy for plaintext ---- #

class Recorder(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.seen.append({"path": self.path, "key": self.headers.get("X-Internal-API-Key")})
        if self.server.redirect_to:
            self.send_response(307)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"status":"ok","active_miners":1,"marked_offline":0}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST


@pytest.fixture
def server_factory():
    servers = []

    def make(redirect_to=None):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        srv.seen, srv.redirect_to = [], redirect_to
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    yield make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_default_transport_refuses_redirects(server_factory, caplog):
    target, target_url = server_factory()
    origin, origin_url = server_factory(redirect_to=target_url + "/api/validator/metagraph-sync")
    ok = main.publish_metagraph_sync(origin_url + "/api", SYNTH_KEY, [(3, "m")], netuid=353, metagraph_block=1)
    assert ok is False
    assert len(origin.seen) == 1 and target.seen == []
    assert "HTTP 307" in caplog.text and SYNTH_KEY not in caplog.text


def test_plaintext_loopback_never_uses_a_proxy(server_factory, monkeypatch):
    proxy, proxy_url = server_factory()
    api, api_url = server_factory()
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert main.publish_metagraph_sync(api_url + "/api", SYNTH_KEY, [(3, "m")], netuid=353, metagraph_block=1)
    assert proxy.seen == []
    assert api.seen == [{"path": "/api/validator/metagraph-sync", "key": SYNTH_KEY}]


# --- side-effect-free modules ------------------------------------------------------ #

PURE_ALLOWED = {
    "__future__", "ipaddress", "json", "re", "urllib", "urllib.error", "urllib.request",
    "urllib.parse", "uuid", "dataclasses", "logging", "api_transport",
}


@pytest.mark.parametrize("module", ["api_transport.py", "deployment_config.py"])
def test_helper_modules_import_only_the_standard_library(module):
    tree = ast.parse((ROOT / module).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
    assert names <= PURE_ALLOWED, sorted(names - PURE_ALLOWED)


def test_importing_helpers_loads_no_sdk_or_runtime(tmp_path):
    code = (
        "import sys, json; sys.path.insert(0, sys.argv[1]);"
        "import api_transport, deployment_config;"
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
        "('bittensor','bittensor_wallet','main','bundle_publish','bundle_consume','weight_loop',"
        "'async_substrate_interface','pydantic'))))"
    )
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], env=env, cwd=tmp_path,
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []
    assert list(tmp_path.iterdir()) == []


def test_metagraph_sync_body_carries_aligned_coldkeys():
    """Each miner's chain coldkey travels with the sync when known, so the API can recognise
    traffic from an account linked to the serving miner's own coldkey."""
    with_keys = json.loads(api_transport.metagraph_sync_body(
        [(3, "hk-a"), (4, "hk-b")], 31, 7200, False, ("ck-a", "ck-b")))
    assert with_keys["miners"] == [{"uid": 3, "hotkey": "hk-a", "coldkey": "ck-a"},
                                   {"uid": 4, "hotkey": "hk-b", "coldkey": "ck-b"}]
    # Without coldkeys (or misaligned ones) the body is exactly as before.
    plain = api_transport.metagraph_sync_body([(3, "hk-a")], 31, 7200, False)
    assert plain == api_transport.metagraph_sync_body([(3, "hk-a")], 31, 7200, False, ("x", "y"))
    assert json.loads(plain)["miners"] == [{"uid": 3, "hotkey": "hk-a"}]


def test_publish_metagraph_sync_sends_coldkeys():
    calls = []
    ok = main.publish_metagraph_sync(
        "https://api.example.invalid/api", SYNTH_KEY, [(3, "miner-hotkey")], netuid=353, metagraph_block=7200,
        urlopen=lambda request, timeout: calls.append(request) or Response(), coldkeys=("miner-coldkey",),
    )
    assert ok is True
    assert json.loads(calls[0].data)["miners"][0]["coldkey"] == "miner-coldkey"
