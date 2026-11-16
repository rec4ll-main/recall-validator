"""Primary/secondary coexistence without weakening signer-specific safety."""

from types import SimpleNamespace

import pytest

import main
import score_version
from environment import EnvironmentVariables
from setter_guard import SetterGuardError, release_process_lock


GENESIS = "0x" + "ab" * 32
HEAD = "0x" + "cd" * 32


class Chain:
    def __init__(self):
        self.substrate = self
        self.genesis = GENESIS
        self.uids = {"master": 0, "secondary": 1}
        self.keys = {0: "master", 1: "secondary"}
        self.permits = [True, True]
        self.queries = []
        self.fail = None

    def get_block_hash(self, block):
        assert block == 0
        return self.genesis

    def get_chain_finalised_head(self):
        if self.fail == "head":
            raise OSError("head unavailable")
        return HEAD

    def get_block_number(self, block_hash):
        assert block_hash == HEAD
        return 10_000

    def get_current_block(self):
        return 10_000

    def query(self, *, module, storage_function, params, block_hash):
        assert module == "SubtensorModule"
        assert block_hash == HEAD
        self.queries.append((storage_function, params, block_hash))
        if self.fail == storage_function:
            raise OSError("storage unavailable")
        if storage_function == "Uids":
            value = self.uids.get(params[1])
        elif storage_function == "Keys":
            value = self.keys.get(params[1])
        elif storage_function == "ValidatorPermit":
            value = self.permits
        else:
            raise AssertionError(storage_function)
        return SimpleNamespace(value=value)


def env(state_dir, role):
    return EnvironmentVariables(
        bundle_mode="chain-v2", allocation_mode="v2", bundle_role=role,
        bundle_state_dir=str(state_dir), master_validator_hotkey_ss58="master",
        bundle_ipfs_api_url="https://ipfs.example.invalid" if role == "master" else "",
        bundle_gateway_urls="https://gateway.example.invalid", network="test", netuid=353,
        allocation_expected_genesis_hash=GENESIS, _env_file=None,
    )


def wallet(hotkey):
    return SimpleNamespace(hotkey=SimpleNamespace(ss58_address=hotkey))


def active_graph():
    return SimpleNamespace(hotkeys=["master", "secondary"],
                           validator_permit=[True, True], last_update=[9999, 9999])


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    yield
    release_process_lock(str(tmp_path / "master"))
    release_process_lock(str(tmp_path / "slave"))


@pytest.mark.parametrize("role,hotkey", [("master", "master"), ("slave", "secondary")])
def test_start_and_restart_with_another_active_distinct_hotkey(tmp_path, role, hotkey):
    chain = Chain()
    settings = env(tmp_path / role, role)
    for _ in range(2):
        main.enforce_v3_startup_guards(chain, active_graph(), wallet(hotkey), settings)
        assert score_version.active_score_version() == "11"
        release_process_lock(settings.bundle_state_dir)
    assert chain.queries and all(q[2] == HEAD for q in chain.queries)


def test_master_and_secondary_may_both_hold_their_own_state_lock(tmp_path):
    chain = Chain()
    main.enforce_v3_startup_guards(chain, active_graph(), wallet("master"), env(tmp_path / "master", "master"))
    main.enforce_v3_startup_guards(chain, active_graph(), wallet("secondary"), env(tmp_path / "slave", "slave"))
    assert score_version.active_score_version() == "11"


@pytest.mark.parametrize("role,hotkey,reason", [
    ("master", "secondary", "master wallet"),
    ("slave", "master", "distinct hotkey"),
    ("slave", "unregistered", "not registered"),
])
def test_wrong_or_reused_signer_is_rejected_before_activation(tmp_path, role, hotkey, reason):
    with pytest.raises(SetterGuardError, match=reason):
        main.enforce_v3_startup_guards(Chain(), active_graph(), wallet(hotkey), env(tmp_path / role, role))


@pytest.mark.parametrize("change,reason", [
    (lambda c: setattr(c, "genesis", "0x" + "ef" * 32), "genesis"),
    (lambda c: c.keys.update({1: "another-hotkey"}), "does not match"),
    (lambda c: c.permits.__setitem__(1, False), "no validator permit"),
    (lambda c: c.permits.__setitem__(1, "true"), "no validator permit"),
    (lambda c: c.uids.pop("master"), "not registered"),
    (lambda c: c.keys.update({0: "wrong-master"}), "does not match"),
])
def test_genesis_registration_master_and_permit_proofs_fail_closed(tmp_path, change, reason):
    chain = Chain()
    change(chain)
    with pytest.raises(SetterGuardError, match=reason):
        main.enforce_v3_startup_guards(chain, active_graph(), wallet("secondary"), env(tmp_path / "slave", "slave"))


@pytest.mark.parametrize("fail", ["head", "Uids", "Keys", "ValidatorPermit"])
def test_unavailable_finalized_proof_cannot_activate_weights_mode(tmp_path, fail):
    chain = Chain()
    chain.fail = fail
    with pytest.raises(SetterGuardError):
        main.enforce_v3_startup_guards(chain, active_graph(), wallet("secondary"), env(tmp_path / "slave", "slave"))


def test_process_lock_failure_cannot_activate_weights_mode(tmp_path, monkeypatch):
    def already_running(state_dir):
        raise SetterGuardError("another process holds the state-dir lock")
    monkeypatch.setattr("setter_guard.acquire_process_lock", already_running)
    with pytest.raises(SetterGuardError, match="state-dir lock"):
        main.enforce_v3_startup_guards(Chain(), active_graph(), wallet("master"), env(tmp_path / "master", "master"))
