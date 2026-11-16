"""registry_bootstrap.py: one finalized snapshot, at most one accepted API update.

Every chain here is a fake and every API is a loopback fake that follows the
recall-web metagraph-sync contract. Nothing reaches a live chain or API.
"""

import ast
import builtins
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import registry_bootstrap as rb
from environment import ENV_FILE_PATH, FINNEY_GENESIS_HASH, MAINNET_MASTER_HOTKEY_SS58, EnvironmentVariables
from registry_bootstrap_fakes import GENESIS, HOTKEYS, MASTER, SYNTH_KEY, FakeApi, FakeChain

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    if ENV_FILE_PATH.exists():
        pytest.skip("a local .env is present; bootstrap tests need the process environment only")
    fields = set(EnvironmentVariables.model_fields)
    for key in tuple(os.environ):
        if key.lower() in fields:
            monkeypatch.delenv(key)


@pytest.fixture
def api():
    servers = []

    def make(**kwargs):
        server = FakeApi(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def configure(monkeypatch, tmp_path, api_url, **overrides):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    values = {
        "NETWORK": "test", "NETUID": "353", "BUNDLE_MODE": "chain-v2", "BUNDLE_ROLE": "master",
        "ALLOCATION_MODE": "v2", "ALLOCATION_EXPECTED_GENESIS_HASH": GENESIS,
        "MASTER_VALIDATOR_HOTKEY_SS58": MASTER, "BUNDLE_STATE_DIR": str(state),
        "BUNDLE_IPFS_API_URL": "https://ipfs.example.invalid", "RECALL_API_URL": api_url + "/api",
        "INTERNAL_API_KEY": SYNTH_KEY,
    }
    values.update(overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return state


def argv(network="test", netuid="353", genesis=GENESIS, master=MASTER, *extra):
    return ["--confirm-registry-write", "--network", network, "--netuid", netuid,
            "--genesis-hash", genesis, "--master-hotkey", master, *extra]


def run(args, chain, capsys):
    code = rb.main(args, subtensor_factory=lambda network: chain.subtensor,
                   metagraph_factory=chain.metagraph_factory)
    captured = capsys.readouterr()
    evidence = json.loads(captured.out.strip().splitlines()[-1]) if captured.out.strip() else None
    return code, evidence, captured


def assert_no_secret(captured):
    text = captured.out + captured.err
    assert SYNTH_KEY not in text and "127.0.0.1" not in text and "example.invalid" not in text
    for hotkey in HOTKEYS:
        assert hotkey not in text


# --- accepted application ---------------------------------------------------------- #

def test_applies_one_finalized_snapshot_to_the_api(monkeypatch, tmp_path, api, capsys):
    server = api()
    server.active = {"retired-miner": 9}            # stale API row from a prior run
    state = configure(monkeypatch, tmp_path, server.url)
    chain = FakeChain()
    code, evidence, captured = run(argv(), chain, capsys)
    assert code == 0, captured.err
    assert server.active == {"owner-hotkey": 0, "miner-a": 2, "miner-b": 3, "miner-c": 4}
    assert len(server.requests) == 1
    headers = {k.lower(): v for k, v in server.requests[0]["headers"].items()}
    assert headers["x-internal-api-key"] == SYNTH_KEY
    assert headers["user-agent"] == "RecallValidator"
    assert not any(k.startswith("epistula-") and k != "epistula-uuid" for k in headers)
    assert evidence == {
        "command": "registry_bootstrap", "result": "applied", "request_attempted": True,
        "netuid": 353, "genesis_hash": GENESIS, "master_source_pinned": False,
        "finalized_block": 10_000, "snapshot_lag_blocks": 0, "uid_count": 5, "miner_count": 4,
        "authoritative_empty": False, "http_status": 200, "api_status": "ok",
        "api_active_miners": 4, "api_marked_offline": 1,
    }
    assert chain.submissions == [] and chain.unfinalized_head_reads == 0
    snapshot_hash = "0x" + f"{10_000:064x}"
    assert {h for _, h in chain.queries} == {snapshot_hash}
    assert chain.metagraph_inits == [{"netuid": 353, "lite": True, "sync": False}]
    assert list(state.iterdir()) == []
    assert_no_secret(captured)


def test_authoritative_empty_needs_explicit_flag(monkeypatch, tmp_path, api, capsys):
    server = api()
    server.active = {"old": 1}
    configure(monkeypatch, tmp_path, server.url)
    code, evidence, _ = run(argv(), FakeChain(["owner-hotkey", MASTER][1:]), capsys)
    assert code == 3 and server.requests == [] and server.active == {"old": 1}
    assert evidence["authoritative_empty"] is True and evidence["request_attempted"] is False
    code, evidence, _ = run(argv("test", "353", GENESIS, MASTER, "--allow-authoritative-empty"),
                            FakeChain([MASTER]), capsys)
    assert code == 0 and server.active == {}
    assert evidence["api_status"] == "ok_empty_authoritative" and evidence["api_marked_offline"] == 1


def test_payload_is_byte_identical_to_the_master_refresh(monkeypatch, tmp_path, api, capsys):
    import main

    server = api()
    configure(monkeypatch, tmp_path, server.url)
    assert run(argv(), FakeChain(), capsys)[0] == 0
    bootstrap_body = server.requests[0]["body"]

    sent = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(main, "send_request", lambda request, timeout, urlopen=None: sent.append(request) or Response())
    chain = FakeChain()
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address=MASTER, sign=lambda message: b"\x00" * 64))
    env = SimpleNamespace(netuid=353, allocation_expected_genesis_hash=GENESIS,
                          master_validator_hotkey_ss58=MASTER, recall_api_url=server.url + "/api",
                          internal_api_key=SYNTH_KEY)
    main._refresh_master_registry(chain.subtensor, wallet, chain.metagraph_factory(
        netuid=353, network="test", sync=False), env)
    assert len(sent) == 1
    assert sent[0].data == bootstrap_body
    assert sent[0].full_url == server.url + "/api/validator/metagraph-sync"


# --- refused before anything is read or sent (exit 2) ------------------------------- #

def forbidden_factory(*a, **k):
    raise AssertionError("chain connection opened during a configuration refusal")


@pytest.mark.parametrize("args,env,reason", [
    (["--network", "test", "--netuid", "353", "--genesis-hash", GENESIS, "--master-hotkey", MASTER],
     {}, "--confirm-registry-write is required"),
    (argv(), {"BUNDLE_ROLE": "slave", "BUNDLE_IPFS_API_URL": "", "BUNDLE_GATEWAY_URLS": "https://gateway.example.invalid"}, "BUNDLE_ROLE must be master"),
    (argv("finney"), {}, "--network does not match"),
    (argv("test", "31"), {}, "--netuid does not match"),
    (argv("test", "353", "0x" + "cd" * 32), {}, "--genesis-hash does not match"),
    (argv("test", "353", GENESIS, "someone-else"), {}, "--master-hotkey does not match"),
    (["--confirm-registry-write"], {}, "are required"),
    (argv(), {"NETUID": None}, "NETUID must be explicitly set"),
    (argv(), {"INTERNAL_API_KEY": "change-me"}, "INTERNAL_API_KEY must be a real secret"),
    (argv(), {"INTERNAL_API_KEY": SYNTH_KEY + "\nextra"}, "INTERNAL_API_KEY contains control characters"),
    (argv(), {"RECALL_API_URL": None}, "RECALL_API_URL must be explicitly set"),
    (argv(), {"RECALL_API_URL": "http://api.example.invalid/api", "ALLOCATION_ALLOW_PLAINTEXT": "true"},
     "must use https"),
    (argv(), {"RECALL_API_URL": f"https://u:{SYNTH_KEY}@api.example.invalid/api"}, "userinfo"),
    (argv("test", "353", GENESIS, MASTER, "--max-snapshot-lag-blocks", "65"), {}, "between 0 and 64"),
    (argv("test", "353", "0x" + "ab" * 31, MASTER), {"ALLOCATION_EXPECTED_GENESIS_HASH": "0x" + "ab" * 31},
     "not a 0x-prefixed 32-byte hash"),
])
def test_configuration_refusals_read_and_send_nothing(monkeypatch, tmp_path, api, capsys, args, env, reason):
    server = api()
    configure(monkeypatch, tmp_path, server.url, **env)
    code = rb.main(args, subtensor_factory=forbidden_factory, metagraph_factory=forbidden_factory)
    captured = capsys.readouterr()
    assert code == 2, captured.err
    assert reason in captured.err
    assert server.requests == []
    assert SYNTH_KEY not in captured.out + captured.err


def test_finney_genesis_requires_the_source_pinned_master(monkeypatch, tmp_path, api, capsys):
    server = api(netuid=31)
    configure(monkeypatch, tmp_path, server.url, NETWORK="finney", NETUID="31",
              ALLOCATION_EXPECTED_GENESIS_HASH=FINNEY_GENESIS_HASH)
    code = rb.main(argv("finney", "31", FINNEY_GENESIS_HASH, MASTER),
                   subtensor_factory=forbidden_factory, metagraph_factory=forbidden_factory)
    # The settings model's mainnet profile refuses it before the bootstrap's own check.
    assert code == 2 and "configuration invalid" in capsys.readouterr().err
    assert server.requests == []


def test_mainnet_name_with_other_genesis_is_refused(monkeypatch, tmp_path, api, capsys):
    server = api()
    configure(monkeypatch, tmp_path, server.url, NETWORK="finney")
    code = rb.main(argv("finney"), subtensor_factory=forbidden_factory, metagraph_factory=forbidden_factory)
    assert code == 2 and "configuration invalid" in capsys.readouterr().err


def configure_mainnet_from_gated_release(monkeypatch, tmp_path, api_url):
    """A file written for the release that gated v3 behind INCENTIVE_V3_MAINNET_31_OPT_IN."""
    configure(monkeypatch, tmp_path, api_url, NETWORK="finney", NETUID="31",
              ALLOCATION_EXPECTED_GENESIS_HASH=FINNEY_GENESIS_HASH,
              MASTER_VALIDATOR_HOTKEY_SS58=MAINNET_MASTER_HOTKEY_SS58,
              INCENTIVE_V3_MODE="weights", INCENTIVE_V3_WEIGHTS_NETUIDS="31",
              INCENTIVE_V3_MAINNET_31_OPT_IN="false",
              INCENTIVE_V3_APPROVED_MAINNET_GENESIS_HASH=FINNEY_GENESIS_HASH,
              INCENTIVE_V3_SLOT_WINDOW_HOURS="48")


def test_mainnet_preparation_from_an_old_gated_file(monkeypatch, tmp_path, api, capsys):
    server = api(netuid=31)
    configure_mainnet_from_gated_release(monkeypatch, tmp_path, server.url)
    hotkeys = ["owner-hotkey", MAINNET_MASTER_HOTKEY_SS58, "miner-a"]
    args = argv("finney", "31", FINNEY_GENESIS_HASH, MAINNET_MASTER_HOTKEY_SS58)
    code, evidence, captured = run(args + ["--evaluate-activated-profile"],
                                   FakeChain(hotkeys, genesis=FINNEY_GENESIS_HASH, netuid=31), capsys)
    assert code == 0, captured.err
    assert "deprecated and has no effect" in captured.err
    assert "activation_flag_in_file" not in evidence and "evaluated_as_activated" not in evidence
    assert evidence["master_source_pinned"] is True and evidence["miner_count"] == 2
    assert os.environ["INCENTIVE_V3_MAINNET_31_OPT_IN"] == "false"
    assert server.active == {"owner-hotkey": 0, "miner-a": 2}


# --- snapshot refusals: nothing sent (exit 3) --------------------------------------- #

def _wrong_genesis(c): c.genesis = "0x" + "ef" * 32
def _master_absent(c): c.hotkeys[1] = "not-the-master"
def _metagraph_netuid(c): c.metagraph_netuid = 354
def _sync_elsewhere(c): c.sync_block_offset = -1
def _incomplete(c): c.subnetwork_n = len(c.hotkeys) + 1
def _keys_mismatch(c): c.keys_override[3] = "replaced-hotkey"
def _uids_mismatch(c): c.uids_override[MASTER] = 4
def _stale(c): c.advance_per_head_read = 4
def _rpc_error(c): c.failing = RuntimeError(f"wss://{SYNTH_KEY}@rpc.example.invalid exploded")
def _duplicate(c): c.hotkeys[4] = "miner-a"


@pytest.mark.parametrize("mutate,reason", [
    (_wrong_genesis, "finalized snapshot refused"),
    (_master_absent, "finalized snapshot refused"),
    (_metagraph_netuid, "finalized snapshot refused"),
    (_sync_elsewhere, "finalized snapshot refused"),
    (_duplicate, "finalized snapshot refused"),
    (_incomplete, "incomplete"),
    (_keys_mismatch, "disagrees with Keys"),
    (_uids_mismatch, "master registration disagrees"),
    (_stale, "stale"),
    (_rpc_error, "RuntimeError during chain reads"),
])
def test_snapshot_refusals_send_nothing(monkeypatch, tmp_path, api, capsys, mutate, reason):
    server = api()
    configure(monkeypatch, tmp_path, server.url)
    chain = FakeChain()
    mutate(chain)
    code, evidence, captured = run(argv(), chain, capsys)
    assert code == 3, captured.err
    assert reason in captured.err
    assert server.requests == [] and evidence["request_attempted"] is False
    assert chain.submissions == []
    assert "rpc.example.invalid" not in captured.out + captured.err
    assert SYNTH_KEY not in captured.out + captured.err


def test_lag_within_bound_is_reported(monkeypatch, tmp_path, api, capsys):
    server = api()
    configure(monkeypatch, tmp_path, server.url)
    chain = FakeChain()
    chain.advance_per_head_read = 3
    code, evidence, _ = run(argv(), chain, capsys)
    assert code == 0 and evidence["snapshot_lag_blocks"] == 3 and evidence["finalized_block"] == 10_003


# --- API outcome refusals: one request at most (exit 4) ------------------------------ #

@pytest.mark.parametrize("override,reason", [
    ((400, {"error": "Metagraph sync netuid must match the configured subnet"}), "HTTP 400"),
    ((500, {"error": "Failed to sync metagraph miners"}), "HTTP 500"),
    ((503, {"error": "Miner store is not configured"}), "HTTP 503"),
    ((200, {"status": "ignored_unqualified_empty", "active_miners": 0, "marked_offline": 0}), "did not apply"),
    ((200, {"status": "ignored_stale_empty", "active_miners": 0, "marked_offline": 0}), "did not apply"),
    ((200, {"status": "ok", "active_miners": 3, "marked_offline": 0}), "different miner count"),
    ((200, {"status": "ok", "active_miners": True, "marked_offline": 0}), "malformed"),
    ((200, {"status": "ok", "active_miners": 4}), "malformed"),
    ((200, b"<html>ok</html>"), "not JSON"),
    ((200, b"[]"), "not a JSON object"),
    ((201, {"status": "ok", "active_miners": 4, "marked_offline": 0}), "HTTP 201"),
    ((204, b""), "HTTP 204"),
])
def test_api_outcomes_other_than_applied_fail(monkeypatch, tmp_path, api, capsys, override, reason):
    server = api(override=override)
    configure(monkeypatch, tmp_path, server.url)
    code, evidence, captured = run(argv(), FakeChain(), capsys)
    assert code == 4, captured.err
    assert reason in captured.err
    assert len(server.requests) == 1 and evidence["request_attempted"] is True
    assert evidence["result"] == "refused"
    assert_no_secret(captured)



def test_duplicate_response_json_keys_are_refused(monkeypatch, tmp_path, api, capsys):
    # Last-wins would turn ignored_* into ok and incorrectly apply; refuse instead.
    raw = (
        b'{"status":"ignored_unqualified_empty","status":"ok",'
        b'"active_miners":4,"marked_offline":0}'
    )
    server = api(override=(200, raw))
    configure(monkeypatch, tmp_path, server.url)
    code, evidence, captured = run(argv(), FakeChain(), capsys)
    assert code == 4 and "duplicate JSON keys" in captured.err
    assert evidence["result"] == "refused" and len(server.requests) == 1
    assert_no_secret(captured)


def test_unknown_api_status_is_not_echoed_into_refusal(monkeypatch, tmp_path, api, capsys):
    leak = "rejected_SYNTHETIC_LEAK_TOKEN_xyz"
    server = api(override=(200, {"status": leak, "active_miners": 0, "marked_offline": 0}))
    configure(monkeypatch, tmp_path, server.url)
    code, evidence, captured = run(argv(), FakeChain(), capsys)
    assert code == 4 and "did not apply" in captured.err
    assert leak not in captured.out + captured.err
    assert SYNTH_KEY not in captured.out + captured.err
    assert_no_secret(captured)


def test_wrong_key_is_refused_by_the_api(monkeypatch, tmp_path, api, capsys):
    server = api(key="a-different-key")
    configure(monkeypatch, tmp_path, server.url)
    code, evidence, captured = run(argv(), FakeChain(), capsys)
    assert code == 4 and "HTTP 401" in captured.err and evidence["http_status"] == 401
    assert server.active == {}


def test_redirect_is_not_followed(monkeypatch, tmp_path, api, capsys):
    target = api()
    origin = api(redirect_to=target.url + "/api/validator/metagraph-sync")
    configure(monkeypatch, tmp_path, origin.url)
    code, evidence, captured = run(argv(), FakeChain(), capsys)
    assert code == 4 and "HTTP 307" in captured.err
    assert len(origin.requests) == 1 and target.requests == []
    assert_no_secret(captured)


def test_unreachable_api_is_a_transport_failure(monkeypatch, tmp_path, api, capsys):
    server = api()
    url = server.url
    server.close()
    configure(monkeypatch, tmp_path, url)
    code, evidence, captured = run(argv(), FakeChain(), capsys)
    assert code == 4 and "connection failed" in captured.err
    assert_no_secret(captured)


# --- no wallet, signing, chain write or filesystem activity ------------------------- #

def test_real_sdk_entry_points_construct_no_wallet_and_write_nothing(monkeypatch, tmp_path, api, capsys):
    """Run through the real ``import bittensor`` path with the SDK classes replaced."""
    import bittensor
    import bittensor_wallet
    from bittensor.core import subtensor as sdk_subtensor
    from bittensor.core.extrinsics import weights as sdk_weights

    def forbidden(*a, **k):
        raise AssertionError("wallet, signing or extrinsic path entered")

    for module, name in [(bittensor, "Wallet"), (bittensor_wallet, "Wallet"),
                         (bittensor_wallet, "Keypair"),
                         (sdk_weights, "set_weights_extrinsic"),
                         (sdk_weights, "commit_timelocked_weights_extrinsic"),
                         (sdk_subtensor.Subtensor, "set_weights"),
                         (sdk_subtensor.Subtensor, "set_commitment"),
                         (sdk_subtensor.Subtensor, "sign_and_send_extrinsic")]:
        monkeypatch.setattr(module, name, forbidden)
    chain = FakeChain()
    monkeypatch.setattr(bittensor, "Subtensor", lambda network: chain.subtensor)
    monkeypatch.setattr(bittensor, "Metagraph", chain.metagraph_factory)

    server = api()
    state = configure(monkeypatch, tmp_path, server.url)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    writes = []
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file, mode="r", *a, **k):
        if any(flag in mode for flag in "wax+"):
            writes.append(str(file))
        return real_open(file, mode, *a, **k)

    def guarded_os_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writes.append(str(path))
        return real_os_open(path, flags, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(os, "mkdir", forbidden)
    monkeypatch.setattr(os, "makedirs", forbidden)

    code = rb.main(argv())
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert writes == []
    assert chain.submissions == []
    assert list(state.iterdir()) == [] and list(home.iterdir()) == []
    assert len(server.requests) == 1



def test_unexpected_transport_error_does_not_echo_secret(monkeypatch, tmp_path, api, capsys):
    """A stdlib-style header ValueError must not leak the key into refusal output."""
    server = api()
    configure(monkeypatch, tmp_path, server.url)
    chain = FakeChain()

    def boom(request, timeout=None):
        raise ValueError(f"Invalid header value b'{SYNTH_KEY}'")

    code = rb.main(
        argv(),
        subtensor_factory=lambda network: chain.subtensor,
        metagraph_factory=chain.metagraph_factory,
        urlopen=boom,
    )
    captured = capsys.readouterr()
    evidence = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 4 and "unexpected transport error" in captured.err
    assert SYNTH_KEY not in captured.out + captured.err
    assert "Invalid header value" not in captured.out + captured.err
    assert evidence["request_attempted"] is True and evidence["result"] == "refused"
    assert server.requests == []


def test_source_imports_no_signing_or_runtime_module():
    tree = ast.parse((ROOT / "registry_bootstrap.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & rb.FORBIDDEN_MODULES
    source = (ROOT / "registry_bootstrap.py").read_text()
    for call in ("Wallet(", ".set_weights(", ".set_commitment(", "acquire_process_lock", "run_validator",
                 "submit_extrinsic(", "sign("):
        assert call not in source, call


_SUBPROCESS_DRIVER = r"""
import json, os, runpy, sys
src = sys.argv[1]
sys.path.insert(0, src)
ns = runpy.run_path(os.path.join(src, "registry_bootstrap.py"), run_name="registry_bootstrap_probe")
from registry_bootstrap_fakes import FakeApi, FakeChain, GENESIS, MASTER, SYNTH_KEY
api = FakeApi()
os.environ.update({
    "NETWORK": "test", "NETUID": "353", "BUNDLE_MODE": "chain-v2", "BUNDLE_ROLE": "master",
    "ALLOCATION_MODE": "v2", "ALLOCATION_EXPECTED_GENESIS_HASH": GENESIS,
    "MASTER_VALIDATOR_HOTKEY_SS58": MASTER, "BUNDLE_STATE_DIR": sys.argv[2],
    "BUNDLE_IPFS_API_URL": "https://ipfs.example.invalid", "RECALL_API_URL": api.url + "/api",
    "INTERNAL_API_KEY": SYNTH_KEY,
})
chain = FakeChain()
code = ns["main"](["--confirm-registry-write", "--network", "test", "--netuid", "353",
                   "--genesis-hash", GENESIS, "--master-hotkey", MASTER],
                  subtensor_factory=lambda network: chain.subtensor,
                  metagraph_factory=chain.metagraph_factory)
api.close()
forbidden = set(ns["FORBIDDEN_MODULES"]) | {"bittensor", "async_substrate_interface"}
print(json.dumps({"exit": code, "applied": len(api.active), "submissions": chain.submissions,
                  "READ_ONLY": os.environ.get("READ_ONLY"),
                  "loaded": sorted(m for m in sys.modules if m.split(".")[0] in forbidden)}))
"""


def _tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def test_isolated_full_run_loads_no_runtime_and_writes_no_files(tmp_path):
    src, home, cwd, state = (tmp_path / n for n in ("src", "home", "cwd", "state"))
    for d in (src, home, cwd, state):
        d.mkdir()
    for py in ROOT.glob("*.py"):
        shutil.copy2(py, src / py.name)
    shutil.copy2(ROOT / "tests" / "registry_bootstrap_fakes.py", src / "registry_bootstrap_fakes.py")
    before = _tree(src)
    env = {"HOME": str(home), "READ_ONLY": "0"}
    out = subprocess.run([sys.executable, "-I", "-c", _SUBPROCESS_DRIVER, str(src), str(state)],
                         env=env, cwd=cwd, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"exit": 0, "applied": 4, "submissions": [], "READ_ONLY": "1", "loaded": []}
    assert _tree(home) == [] and _tree(cwd) == [] and _tree(state) == []
    assert _tree(src) == before


_NOSOCKET_MARKER = "NOSOCKET-SYNTH-" + SYNTH_KEY

_NOSOCKET_CTRL_DRIVER = r"""
import json, os, runpy, sys
src = sys.argv[1]
sys.path.insert(0, src)
ns = runpy.run_path(os.path.join(src, "registry_bootstrap.py"), run_name="rb_ctrl")
from registry_bootstrap_fakes import GENESIS, MASTER, SYNTH_KEY
marker = "NOSOCKET-SYNTH-" + SYNTH_KEY
os.environ.update({
    "NETWORK": "test", "NETUID": "353", "BUNDLE_MODE": "chain-v2", "BUNDLE_ROLE": "master",
    "ALLOCATION_MODE": "v2", "ALLOCATION_EXPECTED_GENESIS_HASH": GENESIS,
    "MASTER_VALIDATOR_HOTKEY_SS58": MASTER, "BUNDLE_STATE_DIR": sys.argv[2],
    "BUNDLE_IPFS_API_URL": "https://ipfs.example.invalid",
    "RECALL_API_URL": "https://api.example.invalid/api",
    "INTERNAL_API_KEY": marker + "\ntrailing",
})

def forbidden(*a, **k):
    raise AssertionError("socket or chain must not open")

code = ns["main"](
    ["--confirm-registry-write", "--network", "test", "--netuid", "353",
     "--genesis-hash", GENESIS, "--master-hotkey", MASTER],
    subtensor_factory=forbidden, metagraph_factory=forbidden, urlopen=forbidden,
)
# Do not print the marker: the assertion is that it never appears in stdout/stderr.
print(json.dumps({"exit": code}))
"""


_NOSOCKET_TRANSPORT_DRIVER = r"""
import json, os, runpy, sys
src = sys.argv[1]
sys.path.insert(0, src)
ns = runpy.run_path(os.path.join(src, "registry_bootstrap.py"), run_name="rb_transport")
from registry_bootstrap_fakes import FakeChain, GENESIS, MASTER, SYNTH_KEY
marker = "NOSOCKET-SYNTH-" + SYNTH_KEY
os.environ.update({
    "NETWORK": "test", "NETUID": "353", "BUNDLE_MODE": "chain-v2", "BUNDLE_ROLE": "master",
    "ALLOCATION_MODE": "v2", "ALLOCATION_EXPECTED_GENESIS_HASH": GENESIS,
    "MASTER_VALIDATOR_HOTKEY_SS58": MASTER, "BUNDLE_STATE_DIR": sys.argv[2],
    "BUNDLE_IPFS_API_URL": "https://ipfs.example.invalid",
    "RECALL_API_URL": "https://api.example.invalid/api",
    "INTERNAL_API_KEY": marker,
})
chain = FakeChain()

def boom(request, timeout=None):
    raise ValueError("Invalid header value b'%s'" % marker)

code = ns["main"](
    ["--confirm-registry-write", "--network", "test", "--netuid", "353",
     "--genesis-hash", GENESIS, "--master-hotkey", MASTER],
    subtensor_factory=lambda network: chain.subtensor,
    metagraph_factory=chain.metagraph_factory,
    urlopen=boom,
)
print(json.dumps({"exit": code, "submissions": chain.submissions}))
"""


def test_nosocket_subprocess_control_char_key_never_echoes(tmp_path):
    src, home, cwd, state = (tmp_path / n for n in ("src", "home", "cwd", "state"))
    for d in (src, home, cwd, state):
        d.mkdir()
    for py in ROOT.glob("*.py"):
        shutil.copy2(py, src / py.name)
    shutil.copy2(ROOT / "tests" / "registry_bootstrap_fakes.py", src / "registry_bootstrap_fakes.py")
    env = {"HOME": str(home), "READ_ONLY": "0"}
    out = subprocess.run(
        [sys.executable, "-I", "-c", _NOSOCKET_CTRL_DRIVER, str(src), str(state)],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["exit"] == 2
    blob = out.stdout + out.stderr
    assert _NOSOCKET_MARKER not in blob
    assert SYNTH_KEY not in blob
    assert "control characters" in out.stderr
    assert _tree(home) == [] and _tree(cwd) == [] and _tree(state) == []


def test_nosocket_subprocess_unexpected_transport_never_echoes(tmp_path):
    src, home, cwd, state = (tmp_path / n for n in ("src", "home", "cwd", "state"))
    for d in (src, home, cwd, state):
        d.mkdir()
    for py in ROOT.glob("*.py"):
        shutil.copy2(py, src / py.name)
    shutil.copy2(ROOT / "tests" / "registry_bootstrap_fakes.py", src / "registry_bootstrap_fakes.py")
    env = {"HOME": str(home), "READ_ONLY": "0"}
    out = subprocess.run(
        [sys.executable, "-I", "-c", _NOSOCKET_TRANSPORT_DRIVER, str(src), str(state)],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["exit"] == 4 and result["submissions"] == []
    blob = out.stdout + out.stderr
    assert _NOSOCKET_MARKER not in blob
    assert SYNTH_KEY not in blob
    assert "Invalid header value" not in blob
    assert "unexpected transport error" in out.stderr
    assert _tree(home) == [] and _tree(cwd) == [] and _tree(state) == []
