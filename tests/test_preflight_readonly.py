"""preflight.py performs chain reads only: no submission, no lock, no file change."""

import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.verified_storage_fake import VerifiedStorageFake

import bundle_consume
import bundle_publish
import main
import preflight
from environment import ENV_FILE_PATH, EnvironmentVariables, FINNEY_GENESIS_HASH, MAINNET_MASTER_HOTKEY_SS58

TEST_GENESIS = "0x" + "ab" * 32
HEAD = "0x" + "cd" * 32
UNFINALIZED = "0x" + "ee" * 32


class Chain(VerifiedStorageFake):
    """Minimal read surface plus recorded (forbidden) submission entry points.

    The finalized state (``HEAD``) and a newer unfinalized state (``UNFINALIZED``)
    deliberately disagree on every field, so a read that is not pinned to the
    finalized hash shows up in the report.
    """

    def __init__(self, genesis, hotkeys):
        self.substrate = self
        self.genesis = genesis
        self.hotkeys = list(hotkeys)
        self.permits = [True] * len(self.hotkeys)
        self.submissions = []
        self.query_hashes = []
        self.head_reads = {"finalized": 0, "unfinalized": 0}
        self.finalized_overrides = {}
        self.unfinalized_hotkeys = list(hotkeys) + ["late-signer"]

    # chain reads
    def get_block_hash(self, block):
        assert block == 0
        return self.genesis

    def get_chain_finalised_head(self):
        self.head_reads["finalized"] += 1
        return HEAD

    def get_chain_head(self):
        self.head_reads["unfinalized"] += 1
        return UNFINALIZED

    def get_block_number(self, block_hash):
        return {HEAD: 10_000, UNFINALIZED: 10_003}[block_hash]

    def get_epoch_schedule_state(self, netuid, block=None):
        return SimpleNamespace(last_epoch_block=9_747, subnet_epoch_index=27,
                               tempo=360, pending_epoch_at=10_108)

    def query(self, *, module, storage_function, params, block_hash=None):
        assert module == "SubtensorModule"
        self.query_hashes.append(block_hash)
        if block_hash == HEAD:
            keys = self.hotkeys
            values = {
                "ValidatorPermit": lambda: self.permits,
                "LastUpdate": lambda: [9_950] * len(keys),
                "LastMechansimStepBlock": lambda: 9_747,
                "MechanismCountCurrent": lambda: 1,
                "WeightsSetRateLimit": lambda: 100,
                "Tempo": lambda: 360,
                "CommitRevealWeightsEnabled": lambda: False,
                "WeightsVersionKey": lambda: 11,
            }
            if storage_function in self.finalized_overrides:
                return SimpleNamespace(value=self.finalized_overrides[storage_function])
        else:  # newer, unfinalized state: every field differs
            keys = self.unfinalized_hotkeys
            values = {
                "ValidatorPermit": lambda: [True] * len(keys),
                "LastUpdate": lambda: [10_002] * len(keys),
                "LastMechansimStepBlock": lambda: 10_001,
                "MechanismCountCurrent": lambda: 2,
                "WeightsSetRateLimit": lambda: 1,
                "Tempo": lambda: 99,
                "CommitRevealWeightsEnabled": lambda: True,
                "WeightsVersionKey": lambda: 12,
            }
        values["Uids"] = lambda: keys.index(params[1]) if params[1] in keys else None
        values["Keys"] = lambda: keys[params[1]] if params[1] < len(keys) else None
        return SimpleNamespace(value=values[storage_function]())

    # forbidden entry points: recorded if ever reached
    def submit_extrinsic(self, *a, **k):
        self.submissions.append("submit_extrinsic")

    def set_weights(self, *a, **k):
        self.submissions.append("set_weights")

    def set_commitment(self, *a, **k):
        self.submissions.append("set_commitment")

    def create_signed_extrinsic(self, *a, **k):
        self.submissions.append("create_signed_extrinsic")

    def sign_and_send_extrinsic(self, *a, **k):
        self.submissions.append("sign_and_send_extrinsic")


def wallet_for(signer):
    def factory(*, name, hotkey, path=None):
        return SimpleNamespace(hotkey=SimpleNamespace(ss58_address=signer))
    return factory


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    if ENV_FILE_PATH.exists():
        pytest.skip("a local .env is present; preflight tests need the process environment only")
    fields = set(EnvironmentVariables.model_fields)
    for key in tuple(os.environ):
        if key.lower() in fields:
            monkeypatch.delenv(key)


def configure_offline_secondary(monkeypatch, state_dir):
    for key, value in {
        "NETWORK": "wss://rpc.example.invalid", "NETUID": "99", "BUNDLE_MODE": "chain-v2",
        "BUNDLE_ROLE": "slave",
        "ALLOCATION_MODE": "v2", "ALLOCATION_EXPECTED_GENESIS_HASH": TEST_GENESIS,
        "MASTER_VALIDATOR_HOTKEY_SS58": "master", "BUNDLE_STATE_DIR": str(state_dir),
        "BUNDLE_GATEWAY_URLS": "https://gateway.example.invalid",
    }.items():
        monkeypatch.setenv(key, value)


def configure_mainnet_secondary_from_gated_release(monkeypatch, state_dir):
    """A file written for the release that gated v3 behind INCENTIVE_V3_MAINNET_31_OPT_IN."""
    for key, value in {
        "NETWORK": "finney", "NETUID": "31", "BUNDLE_MODE": "chain-v2", "BUNDLE_ROLE": "slave",
        "ALLOCATION_MODE": "v2", "ALLOCATION_EXPECTED_GENESIS_HASH": FINNEY_GENESIS_HASH,
        "MASTER_VALIDATOR_HOTKEY_SS58": MAINNET_MASTER_HOTKEY_SS58,
        "BUNDLE_STATE_DIR": str(state_dir), "BUNDLE_GATEWAY_URLS": "https://gateway.example.invalid",
        "INCENTIVE_V3_MODE": "weights", "INCENTIVE_V3_WEIGHTS_NETUIDS": "31",
        "INCENTIVE_V3_MAINNET_31_OPT_IN": "false",
        "INCENTIVE_V3_APPROVED_MAINNET_GENESIS_HASH": FINNEY_GENESIS_HASH,
        "INCENTIVE_V3_SLOT_WINDOW_HOURS": "48",
    }.items():
        monkeypatch.setenv(key, value)


def run(argv, chain, hotkey):
    return preflight.main(argv, wallet_factory=wallet_for(hotkey),
                          subtensor_factory=lambda network: chain)


def test_secondary_preflight_passes_with_reads_only(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    assert run([], chain, "secondary") == 0
    out = capsys.readouterr().out
    assert "READ-ONLY PREFLIGHT PASSED" in out
    assert '"signer_uid": 2' in out and '"weights_rate_limit": 100' in out
    assert chain.submissions == []
    assert list(tmp_path.iterdir()) == []  # no lock file or other state created


def test_guard_replaces_every_submission_entry_point():
    chain = Chain(TEST_GENESIS, ["master"])
    preflight.guard_submissions(chain)
    for call in (chain.submit_extrinsic, chain.set_weights, chain.set_commitment,
                 chain.create_signed_extrinsic, chain.sign_and_send_extrinsic):
        with pytest.raises(preflight.SubmissionRefused):
            call()
    assert chain.submissions == []


def test_guard_covers_real_sdk_write_surface():
    """Every SDK write helper funnels through these; keep the list honest."""
    import bittensor as bt
    from async_substrate_interface.sync_substrate import SubstrateInterface

    for attr in ("set_weights", "set_commitment", "sign_and_send_extrinsic", "mev_submit_encrypted"):
        assert callable(getattr(bt.Subtensor, attr)), attr
    for attr in preflight._SUBSTRATE_SUBMIT_ATTRS:
        assert callable(getattr(SubstrateInterface, attr)), attr


def test_runtime_and_signing_paths_are_never_entered(monkeypatch, tmp_path):
    configure_offline_secondary(monkeypatch, tmp_path)

    def forbidden(*a, **k):
        raise AssertionError("runtime path entered during preflight")

    for module, name in [(main, "run_validator"), (main, "run_chain_v2_tick"),
                         (main, "run_bundle_epoch"), (main, "enforce_v3_startup_guards"),
                         (bundle_publish, "master_publish"), (bundle_consume, "slave_consume")]:
        monkeypatch.setattr(module, name, forbidden)
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    assert run([], chain, "secondary") == 0


def test_old_gated_mainnet_file_is_checked_as_the_v11_profile(monkeypatch, tmp_path, capsys):
    # The removed opt-in no longer gates anything: preflight evaluates the profile the
    # validator will run, and the old evaluation flag is accepted with no effect.
    configure_mainnet_secondary_from_gated_release(monkeypatch, tmp_path)
    chain = Chain(FINNEY_GENESIS_HASH, ["owner", MAINNET_MASTER_HOTKEY_SS58, "secondary"])
    assert run([], chain, "secondary") == 0
    out = capsys.readouterr().out
    assert '"score_version": "11"' in out and "activation_flag_in_file" not in out
    assert run(["--evaluate-activated-profile"], chain, "secondary") == 0
    captured = capsys.readouterr()
    assert "deprecated and has no effect" in captured.err
    assert os.environ["INCENTIVE_V3_MAINNET_31_OPT_IN"] == "false"
    assert chain.submissions == []


def test_removed_off_mode_fails_configuration(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    monkeypatch.setenv("INCENTIVE_V3_MODE", "off")
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    assert run([], chain, "secondary") == 2
    assert chain.submissions == []


def test_held_state_lock_fails_without_taking_it(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    lock = tmp_path / preflight.LOCK_NAME
    lock.write_text("4242")
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
        assert run([], chain, "secondary") == 3
        assert "holds the state-dir lock" in capsys.readouterr().err
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert lock.read_text() == "4242"


def test_unheld_existing_lock_is_left_unchanged(monkeypatch, tmp_path):
    configure_offline_secondary(monkeypatch, tmp_path)
    lock = tmp_path / preflight.LOCK_NAME
    lock.write_text("1")
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    assert run([], chain, "secondary") == 0
    assert lock.read_text() == "1"


def test_relative_state_dir_is_refused(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    monkeypatch.setenv("BUNDLE_STATE_DIR", "./bundle_state")
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    assert run([], chain, "secondary") == 3
    assert "absolute path" in capsys.readouterr().err


@pytest.mark.parametrize("hotkey,chain_keys,message", [
    ("master", ["owner", "master", "secondary"], "distinct hotkey"),
    ("unregistered", ["owner", "master", "secondary"], "not registered"),
])
def test_signer_identity_failures(monkeypatch, tmp_path, capsys, hotkey, chain_keys, message):
    configure_offline_secondary(monkeypatch, tmp_path)
    chain = Chain(TEST_GENESIS, chain_keys)
    assert run([], chain, hotkey) == 3
    assert message in capsys.readouterr().err
    assert chain.submissions == []


def test_missing_permit_fails(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    chain.permits = [True, True, False]
    assert run([], chain, "secondary") == 3
    assert "validator permit" in capsys.readouterr().err


def test_wrong_genesis_fails(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    chain = Chain("0x" + "ef" * 32, ["owner", "master", "secondary"])
    assert run([], chain, "secondary") == 3
    assert chain.submissions == []


def test_configuration_errors_do_not_echo_values(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    monkeypatch.setenv("ALLOCATION_MODE", "sekrit-value-must-not-print")
    assert run([], Chain(TEST_GENESIS, ["master"]), "secondary") == 2
    captured = capsys.readouterr()
    assert "sekrit-value-must-not-print" not in captured.out + captured.err


def test_master_needs_real_internal_key(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    monkeypatch.setenv("BUNDLE_ROLE", "master")
    monkeypatch.setenv("BUNDLE_IPFS_API_URL", "https://ipfs.example.invalid")
    monkeypatch.setenv("RECALL_API_URL", "https://api.example.invalid/api")
    monkeypatch.setenv("INTERNAL_API_KEY", "change-me")
    assert run([], Chain(TEST_GENESIS, ["owner", "master"]), "master") == 2
    err = capsys.readouterr().err
    assert "INTERNAL_API_KEY must be a real secret" in err and "change-me" not in err


def test_preflight_forces_sdk_read_only_before_any_other_import():
    assert os.environ.get("READ_ONLY") == "1"
    source = open(preflight.__file__).read()
    forced = source.index('os.environ["READ_ONLY"] = "1"')
    assert source.index("sys.dont_write_bytecode = True") < source.index("import argparse")
    assert 'setdefault("READ_ONLY"' not in source
    # Only ``os``/``sys`` (and __future__) are imported before the forced settings.
    head = source[:forced]
    imports = [line for line in head.splitlines() if line.startswith(("import ", "from "))]
    assert imports == ["from __future__ import annotations", "import os", "import sys"]


def test_every_reported_field_is_read_at_one_finalized_hash(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    assert run([], chain, "secondary") == 0
    report = json.loads(capsys.readouterr().out.split("READ-ONLY PREFLIGHT PASSED")[0])
    assert chain.head_reads == {"finalized": 1, "unfinalized": 0}
    assert chain.query_hashes and set(chain.query_hashes) == {HEAD}
    assert report["finalized_hash"] == HEAD and report["finalized_block"] == 10_000
    assert report["all_reads_at_finalized_hash"] is True
    assert (report["tempo"], report["weights_rate_limit"], report["weights_version_key"]) == (360, 100, 11)
    assert report["commit_reveal_enabled"] is False and report["mechanism_count"] == 1
    assert report["mechanisms"] == [{"mechid": 0, "last_update": 9_950, "blocks_since_last_update": 50,
                                      "last_update_defaulted": False, "rate_limit_allows_weights": False}]
    assert (report["signer_uid"], report["master_uid"]) == (2, 1)
    assert report["bundle_epoch_start_block"] == 9_720 and report["bundle_cutoff_block"] == 9_732
    assert chain.submissions == []


def test_registration_visible_only_in_unfinalized_state_fails(monkeypatch, tmp_path, capsys):
    configure_offline_secondary(monkeypatch, tmp_path)
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    assert run([], chain, "late-signer") == 3
    assert "not registered" in capsys.readouterr().err
    assert chain.head_reads["unfinalized"] == 0 and set(chain.query_hashes) == {HEAD}


@pytest.mark.parametrize("field,value", [
    ("Tempo", None), ("Tempo", 0), ("WeightsSetRateLimit", True),
    ("CommitRevealWeightsEnabled", None), ("WeightsVersionKey", "11"),
])
def test_unreadable_finalized_cadence_field_fails(monkeypatch, tmp_path, capsys, field, value):
    configure_offline_secondary(monkeypatch, tmp_path)
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    chain.finalized_overrides[field] = value
    assert run([], chain, "secondary") == 3
    assert field in capsys.readouterr().err
    assert chain.submissions == []



@pytest.mark.parametrize("field,value,reason", [
    ("WeightsSetRateLimit", -1, "WeightsSetRateLimit must be non-negative"),
    ("MechanismCountCurrent", -2, "MechanismCountCurrent must be positive"),
    ("MechanismCountCurrent", 0, "MechanismCountCurrent must be positive"),
    ("LastUpdate", [10_500, 10_500, 10_500], "LastUpdate is in the future"),
    ("LastUpdate", [9_950, 9_950, -1], "LastUpdate must be non-negative"),
])
def test_impossible_finalized_cadence_values_fail(monkeypatch, tmp_path, capsys, field, value, reason):
    """Malformed/future LastUpdate and negative rate/mechanism count are blocking."""
    configure_offline_secondary(monkeypatch, tmp_path)
    chain = Chain(TEST_GENESIS, ["owner", "master", "secondary"])
    chain.finalized_overrides[field] = value
    assert run([], chain, "secondary") == 3
    err = capsys.readouterr().err
    assert reason in err
    assert "READ-ONLY PREFLIGHT PASSED" not in err
    assert chain.submissions == []
    assert HEAD in chain.query_hashes



_FS_DRIVER = r"""
import json, os, runpy, sys
src = sys.argv[1]
sys.path.insert(0, src)
ns = runpy.run_path(os.path.join(src, "preflight.py"), run_name="preflight_probe")
import bittensor, bittensor_wallet, async_substrate_interface  # the SDK the preflight uses
import environment, deployment_config, bundle_schedule, setter_guard, validator_signer_guard, weight_loop
from bittensor.core import settings
code = ns["main"]([])  # no configuration: exits 2 without touching the chain
print(json.dumps({"exit": code, "READ_ONLY": os.environ.get("READ_ONLY"),
                  "sdk_read_only": settings.READ_ONLY, "no_bytecode": sys.dont_write_bytecode}))
"""


def _tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


@pytest.mark.parametrize("inherited", [
    pytest.param({"READ_ONLY": "0"}, id="inherited-READ_ONLY-0"),
    pytest.param({}, id="no-environment"),
])
def test_isolated_subprocess_creates_no_files(tmp_path, inherited):
    """Real SDK import in a clean interpreter: no ~/.bittensor dirs, no bytecode."""
    import shutil
    import subprocess
    import sys

    root = Path(preflight.__file__).resolve().parent
    src, home, cwd = tmp_path / "src", tmp_path / "home", tmp_path / "cwd"
    for d in (src, home, cwd):
        d.mkdir()
    for py in root.glob("*.py"):
        shutil.copy2(py, src / py.name)
    before = _tree(src)
    env = {"HOME": str(home), **inherited}
    out = subprocess.run([sys.executable, "-I", "-c", _FS_DRIVER, str(src)], env=env, cwd=cwd,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"exit": 2, "READ_ONLY": "1", "sdk_read_only": True, "no_bytecode": True}
    assert _tree(home) == []          # no ~/.bittensor/wallets or ~/.bittensor/miners
    assert _tree(cwd) == []
    assert _tree(src) == before       # no __pycache__ written into the checkout


def test_endpoint_report_strips_credentials():
    assert preflight.public_endpoint("wss://user:pw@rpc.example.invalid:443/x?token=1") == "wss://rpc.example.invalid:443"
    assert preflight.public_endpoint("finney") == "finney"
