"""Offline regressions for runtime-compatible first-use LastUpdate semantics.

All chain state is fake. No test opens a wallet, contacts an RPC endpoint, signs,
or broadcasts. A zero LastUpdate exempts only the cadence rate-limit check.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from scalecodec.utils.ss58 import ss58_encode

import preflight
from bundle_schedule import FinalizedHead
from chain_cadence import CadenceReadError
from weight_loop import LiveParams, SubstrateReader, SubmitResult, WeightLoop, WeightVector


NETUID = 353
# Public byte strings only: no keypairs, wallets, private keys, or signing.
SIGNER = ss58_encode("11" * 32, ss58_format=42)
MASTER = ss58_encode("22" * 32, ss58_format=42)
MINER = ss58_encode("33" * 32, ss58_format=42)
FINALIZED = "0x" + "ab" * 32
UNFINALIZED = "0x" + "cd" * 32


class LiveReader:
    """Already-validated adapter fake for exercising the loop's own gates."""

    def __init__(self, **changes):
        self.params = replace(LiveParams(
            head=10, rate_limit=1000, tempo=360, commit_reveal=False,
            version_key=11, mechanism_count=1, uid=2, permit=True,
            last_update=0, epoch_marker=0,
        ), **changes)
        self.reads = []
        self.keys = {0: MASTER, 1: MINER, 2: SIGNER}
        self.per_mechanism = {}

    def mechanism_count(self, netuid):
        assert netuid == NETUID
        return self.params.mechanism_count

    def read(self, netuid, hotkey, mechid):
        assert (netuid, hotkey) == (NETUID, SIGNER)
        self.reads.append(mechid)
        return replace(self.params, last_update=self.per_mechanism.get(mechid, self.params.last_update))

    def hotkey_at(self, netuid, uid):
        assert netuid == NETUID
        return self.keys.get(uid)

    def last_update(self, netuid, mechid, uid):
        raise AssertionError("a durable status must not use the legacy confirmation probe")


class RecordingSubmit:
    def __init__(self, *outcomes):
        self.calls = []
        self.outcomes = list(outcomes)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcomes.pop(0) if self.outcomes else SubmitResult(True, "fake finalized application", "confirmed")


def valid_vector(head=0):
    return WeightVector(uids=(1,), weights_u16=(65535,), hotkeys=(MINER,),
                        as_of_block=head, bundle_seq=0)


def make_loop(reader, submit=None, vector=None):
    submit = submit if submit is not None else RecordingSubmit()
    vector = valid_vector() if vector is None else vector
    loop = WeightLoop(reader=reader, submit=submit, weights_source=lambda: vector,
                      netuid=NETUID, hotkey=SIGNER)
    return loop, submit


@pytest.mark.parametrize("head,rate", [(0, 1000), (1, 1000), (10, 1000), (10, 0)])
def test_never_set_zero_is_due_even_below_rate_limit(head, rate):
    loop, submit = make_loop(LiveReader(head=head, rate_limit=rate, last_update=0))
    [result] = loop.tick()
    assert result.status == "confirmed", result
    assert len(submit.calls) == 1


@pytest.mark.parametrize("head,rate,expected", [
    (109, 100, "not_due"), (110, 100, "confirmed"),
    (111, 100, "confirmed"), (10, 0, "confirmed"),
])
def test_positive_last_update_keeps_exact_rate_boundary(head, rate, expected):
    loop, submit = make_loop(LiveReader(head=head, rate_limit=rate, last_update=10))
    [result] = loop.tick()
    assert result.status == expected, result
    assert len(submit.calls) == (expected == "confirmed")


def test_mechanisms_have_independent_zero_and_positive_cadence():
    reader = LiveReader(head=10, rate_limit=1000, mechanism_count=2)
    reader.per_mechanism = {0: 0, 1: 1}
    loop, submit = make_loop(reader)
    assert [result.status for result in loop.tick()] == ["confirmed", "not_due"]
    assert [call["mechid"] for call in submit.calls] == [0]


@pytest.mark.parametrize("field,value", [
    ("last_update", None), ("last_update", -1), ("last_update", 11),
    ("last_update", True), ("last_update", False),
    ("last_update", "0"), ("last_update", 0.0), ("last_update", 1 << 64),
    ("rate_limit", None), ("rate_limit", -1), ("rate_limit", True),
    ("rate_limit", "0"), ("rate_limit", 0.0), ("rate_limit", 1 << 64),
    ("mechanism_count", None), ("mechanism_count", 0), ("mechanism_count", -1),
    ("mechanism_count", True), ("mechanism_count", "1"), ("mechanism_count", 1.0),
    ("mechanism_count", 17),
    ("uid", None), ("uid", -1), ("uid", 65536), ("uid", True),
    ("uid", "2"), ("uid", 2.0),
    ("permit", False), ("permit", None), ("permit", 1), ("permit", "true"),
    ("permit", 1.0),
])
def test_zero_sentinel_never_coerces_or_bypasses_invalid_live_params(field, value):
    loop, submit = make_loop(LiveReader(**{field: value}))
    results = loop.tick()
    assert results and all(result.status in {"error", "alert"} for result in results), results
    assert submit.calls == []


@pytest.mark.parametrize("bad_vector", [
    WeightVector(uids=(1,), weights_u16=(65535,), hotkeys=("wrong-miner",), as_of_block=0, bundle_seq=0),
    WeightVector(uids=(1,), weights_u16=(65535,), hotkeys=(MINER,), as_of_block=11, bundle_seq=0),
    WeightVector(uids=(1,), weights_u16=(0,), hotkeys=(MINER,), as_of_block=0, bundle_seq=0),
])
def test_zero_sentinel_still_checks_verified_vector(bad_vector):
    loop, submit = make_loop(LiveReader(), vector=bad_vector)
    assert loop.tick()[0].status == "failclosed"
    assert submit.calls == []


def test_zero_sentinel_cannot_dispatch_without_verified_or_fallback_vector():
    loop, submit = make_loop(LiveReader())
    loop.weights_source = lambda: None
    assert loop.tick()[0].status == "failclosed"
    assert submit.calls == []


@pytest.mark.parametrize("status", ["waiting", "pending_reveal"])
def test_zero_sentinel_preserves_pending_reconciliation_fence(status):
    reader = LiveReader()
    submit = RecordingSubmit()
    reconciled = []

    def reconcile(mechid):
        reconciled.append(mechid)
        return SubmitResult(False, "existing durable fence", status)

    submit.reconcile = reconcile
    loop, _ = make_loop(reader, submit)
    assert loop.tick()[0].status == status
    assert reconciled == [0]
    assert reader.reads == []
    assert submit.calls == []


def test_zero_sentinel_preserves_reconciliation_failure_fence():
    reader = LiveReader()
    submit = RecordingSubmit()

    def reconcile(mechid):
        raise OSError("offline reconciliation read failed")

    submit.reconcile = reconcile
    loop, _ = make_loop(reader, submit)
    assert loop.tick()[0].status == "error"
    assert reader.reads == []
    assert submit.calls == []


def test_zero_sentinel_respects_backoff_and_max_transient_attempts():
    reader = LiveReader()
    submit = RecordingSubmit(*[SubmitResult(False, "offline fake timeout")] * 4)
    loop, _ = make_loop(reader, submit)
    assert loop.tick()[0].status == "error"
    assert loop.tick()[0].status == "waiting"
    assert len(submit.calls) == 1
    reader.params = replace(reader.params, head=12)
    assert loop.tick()[0].status == "error"
    reader.params = replace(reader.params, head=16)
    assert loop.tick()[0].status == "error"
    reader.params = replace(reader.params, head=24)
    assert loop.tick()[0].status == "waiting"
    assert len(submit.calls) == 3


def test_zero_sentinel_does_not_bypass_setting_weights_too_fast_backoff():
    reader = LiveReader(rate_limit=100)
    submit = RecordingSubmit(SubmitResult(False, "SettingWeightsTooFast"))
    loop, _ = make_loop(reader, submit)
    assert loop.tick()[0].status == "waiting"
    assert loop.tick()[0].status == "waiting"
    assert len(submit.calls) == 1
    reader.params = replace(reader.params, head=101)
    assert loop.tick()[0].status == "confirmed"
    assert len(submit.calls) == 2


class FakeStorageKey:
    """StorageKey's decode API, including the SDK's dangerous null default."""

    def __init__(self, chain, name, params, block_hash):
        self.chain, self.name, self.params, self.block_hash = chain, name, params, block_hash
        self.metadata_storage_function = SimpleNamespace(value={
            "modifier": chain.modifiers.get(name, "Default" if name == "LastUpdate" else "Optional"),
        })
        self.value_scale_type = "Vec<u64>" if name == "LastUpdate" else "fake"

    def to_hex(self):
        return "0x" + json.dumps([self.name, self.params, self.block_hash]).encode().hex()

    def decode_scale_value(self, data=None):
        self.chain.decoded.append((self.name, self.block_hash, data is None))
        if self.name in self.chain.decoded_overrides:
            return SimpleNamespace(value=self.chain.decoded_overrides[self.name])
        if data is None:
            value = [] if self.name == "LastUpdate" else None
        else:
            raw = bytes(data.data)
            if self.name == "LastUpdate":
                if not raw or raw[0] % 4:
                    raise ValueError("invalid fake SCALE Vec<u64> prefix")
                count = raw[0] >> 2
                if len(raw) != 1 + count * 8:
                    raise ValueError("truncated or trailing fake SCALE Vec<u64>")
                value = [int.from_bytes(raw[1 + i * 8:9 + i * 8], "little") for i in range(count)]
            else:
                value = json.loads(raw)
        return SimpleNamespace(value=value)


class FinalizedStorage:
    """Finalized and unfinalized fake states deliberately disagree everywhere.

    LastUpdate uses actual short SCALE Vec<u64> bytes. The other fake storage
    values use JSON solely to preserve their exact Python types through decoding.
    """

    def __init__(self):
        self.substrate = self
        self.head = 10
        self.head_hash = FINALIZED
        self.head_reads = {"finalized": 0, "unfinalized": 0}
        self.query_calls = []
        self.key_calls = []
        self.rpc_calls = []
        self.decoded = []
        self.overrides = {}
        self.decoded_overrides = {}
        self.rpc_overrides = {}
        self.modifiers = {}
        self.rows = {NETUID: [0, 0, 0]}
        self.keys = {}
        self.submissions = []

    def get_chain_finalised_head(self):
        self.head_reads["finalized"] += 1
        return self.head_hash

    def get_chain_head(self):
        self.head_reads["unfinalized"] += 1
        return UNFINALIZED

    def get_block_number(self, block_hash):
        return self.head if block_hash == self.head_hash else self.head + 3

    def get_block_hash(self, block):
        assert block == 0
        return "0x" + "01" * 32

    def value(self, name, params, block_hash):
        if block_hash == self.head_hash:
            keys = [MASTER, MINER, SIGNER]
            values = {
                "ValidatorPermit": [True, True, True], "MechanismCountCurrent": 1,
                "WeightsSetRateLimit": 1000, "Tempo": 360,
                "CommitRevealWeightsEnabled": False, "WeightsVersionKey": 11,
                "LastMechansimStepBlock": 0, "SubnetOwnerHotkey": "subnet-owner",
            }
            if name in self.overrides:
                return self.overrides[name]
            if name == "LastUpdate":
                return self.rows.get(params[0], [])
        else:
            keys = ["unfinalized-master", "unfinalized-miner", "unfinalized-signer"]
            values = {
                "ValidatorPermit": [False, False, False], "MechanismCountCurrent": 3,
                "WeightsSetRateLimit": 1, "Tempo": 99,
                "CommitRevealWeightsEnabled": True, "WeightsVersionKey": 99,
                "LastMechansimStepBlock": 12, "LastUpdate": [12, 12, 12],
                "SubnetOwnerHotkey": "unfinalized-owner",
            }
        if name == "Uids":
            return keys.index(params[1]) if params[1] in keys else None
        if name == "Keys":
            return keys[params[1]] if 0 <= params[1] < len(keys) else None
        return values[name]

    def query(self, *, module, storage_function, params, block_hash=None):
        assert module == "SubtensorModule"
        self.query_calls.append((storage_function, tuple(params), block_hash))
        return SimpleNamespace(value=self.value(storage_function, params, block_hash))

    def create_storage_key(self, pallet, storage_function, params=None, block_hash=None):
        assert pallet == "SubtensorModule"
        self.key_calls.append((storage_function, tuple(params), block_hash))
        key = FakeStorageKey(self, storage_function, params, block_hash)
        self.keys[key.to_hex()] = key
        return key

    def rpc_request(self, method, params):
        assert method in {"state_getStorage", "state_getStorageAt"}
        key = self.keys[params[0]]
        assert len(params) == 2 and params[1] == key.block_hash
        self.rpc_calls.append((key.name, tuple(key.params), params[1]))
        if key.name in self.rpc_overrides:
            response = self.rpc_overrides[key.name]
            if isinstance(response, Exception):
                raise response
            return response
        value = self.value(key.name, key.params, params[1])
        if key.name == "LastUpdate":
            assert isinstance(value, (list, tuple)) and len(value) < 64
            raw = bytes([len(value) << 2]) + b"".join(v.to_bytes(8, "little") for v in value)
        else:
            raw = json.dumps(value).encode()
        return {"jsonrpc": "2.0", "id": 1, "result": "0x" + raw.hex()}

    def submit_extrinsic(self, *args, **kwargs):
        self.submissions.append((args, kwargs))
        raise AssertionError("no chain submission is permitted in compatibility tests")


def finalized_report(chain):
    return preflight.read_finalized_fields(
        chain, NETUID, SIGNER, MASTER, FinalizedHead(chain.head, chain.head_hash),
    )


@pytest.mark.parametrize("row", [[], [0], [1, 2], (0, 0)])
def test_successfully_queried_short_vector_is_zero_in_runtime_and_preflight(row):
    chain = FinalizedStorage()
    chain.rows[NETUID] = row
    params = SubstrateReader(chain).read(NETUID, SIGNER, 0)
    assert (params.uid, params.permit, params.last_update) == (2, True, 0)
    report = finalized_report(chain)
    assert report["mechanisms"] == [{
        "mechid": 0, "last_update": 0, "last_update_defaulted": True,
        "blocks_since_last_update": 10, "rate_limit_allows_weights": True,
    }]
    assert chain.rpc_calls and {call[2] for call in chain.rpc_calls} == {FINALIZED}
    assert chain.submissions == []


def test_successful_rpc_null_uses_proven_last_update_default():
    chain = FinalizedStorage()
    chain.rpc_overrides["LastUpdate"] = {"jsonrpc": "2.0", "id": 1, "result": None}
    params = SubstrateReader(chain).read(NETUID, SIGNER, 0)
    assert params.last_update == 0
    assert finalized_report(chain)["mechanisms"][0]["last_update"] == 0
    assert ("LastUpdate", FINALIZED, True) in chain.decoded


def test_null_without_default_metadata_is_not_never_set():
    chain = FinalizedStorage()
    chain.modifiers["LastUpdate"] = "Optional"
    chain.rpc_overrides["LastUpdate"] = {"jsonrpc": "2.0", "id": 1, "result": None}
    with pytest.raises(CadenceReadError):
        SubstrateReader(chain).read(NETUID, SIGNER, 0)
    with pytest.raises(preflight.PreflightError):
        finalized_report(chain)


@pytest.mark.parametrize("response", [
    None, [], {}, {"jsonrpc": "2.0", "id": 1},
    {"error": {"code": -32000, "message": "offline fixture RPC error"}},
    {"error": {"code": -32000}, "result": None},
    {"result": 0}, {"result": []}, {"result": "0xzz"}, {"result": "0x0"},
    {"result": "0x"}, {"result": "0x04"},
    OSError("offline fixture transport failure"),
])
def test_rpc_failures_and_malformed_bytes_are_never_converted_to_zero(response):
    chain = FinalizedStorage()
    chain.rpc_overrides["LastUpdate"] = response
    loop, submit = make_loop(SubstrateReader(chain))
    results = loop.tick()
    assert results and all(result.status in {"error", "alert"} for result in results), results
    assert submit.calls == []
    with pytest.raises(preflight.PreflightError):
        finalized_report(chain)
    assert chain.submissions == []


@pytest.mark.parametrize("row", [
    None, {}, "", "0", 0, False, [0, 0, -1], [0, 0, 11],
    [0, 0, False], [0, 0, True], [0, 0, "0"], [0, 0, 0.0],
    [None], [True], ["0"], [-1], [11], [1 << 64],
])
def test_malformed_or_impossible_decoded_vector_is_not_a_default(row):
    chain = FinalizedStorage()
    chain.decoded_overrides["LastUpdate"] = row
    loop, submit = make_loop(SubstrateReader(chain))
    results = loop.tick()
    assert results and all(result.status in {"error", "alert"} for result in results), results
    assert submit.calls == []
    with pytest.raises(preflight.PreflightError):
        finalized_report(chain)


@pytest.mark.parametrize("field,value", [
    ("Uids", None), ("Uids", -1), ("Uids", 65536), ("Uids", True),
    ("Uids", "2"), ("Uids", 2.0),
    ("Keys", "different-hotkey"), ("Keys", None),
    ("ValidatorPermit", None), ("ValidatorPermit", []),
    ("ValidatorPermit", [True, True, False]), ("ValidatorPermit", [True, True, 1]),
    ("ValidatorPermit", [True, True, "true"]), ("ValidatorPermit", [True, True, 1.0]),
    ("MechanismCountCurrent", None), ("MechanismCountCurrent", 0),
    ("MechanismCountCurrent", -1), ("MechanismCountCurrent", True),
    ("MechanismCountCurrent", "1"), ("MechanismCountCurrent", 1.0),
    ("MechanismCountCurrent", 17),
    ("WeightsSetRateLimit", None), ("WeightsSetRateLimit", -1),
    ("WeightsSetRateLimit", True), ("WeightsSetRateLimit", "1"),
    ("WeightsSetRateLimit", 1.0), ("WeightsSetRateLimit", 1 << 64),
])
def test_empty_vector_requires_valid_identity_mechanism_permit_and_rate(field, value):
    chain = FinalizedStorage()
    chain.rows[NETUID] = []
    chain.overrides[field] = value
    loop, submit = make_loop(SubstrateReader(chain))
    results = loop.tick()
    assert results and all(result.status in {"error", "alert"} for result in results), results
    assert submit.calls == []
    with pytest.raises(preflight.PreflightError):
        finalized_report(chain)


@pytest.mark.parametrize("mechid", [-1, 1, True, "0", 0.0])
def test_empty_vector_cannot_make_an_inactive_or_malformed_mechanism_valid(mechid):
    chain = FinalizedStorage()
    chain.rows[NETUID] = []
    with pytest.raises(CadenceReadError):
        SubstrateReader(chain).read(NETUID, SIGNER, mechid)


def test_runtime_snapshot_and_preflight_use_one_exact_finalized_hash_for_all_mechanisms():
    chain = FinalizedStorage()
    chain.overrides["MechanismCountCurrent"] = 2
    chain.rows = {NETUID: [], NETUID + 4096: [1, 1, 1]}
    loop, submit = make_loop(SubstrateReader(chain))
    assert [result.status for result in loop.tick()] == ["confirmed", "not_due"]
    assert chain.head_reads == {"finalized": 1, "unfinalized": 0}
    calls = chain.key_calls + chain.rpc_calls + chain.query_calls
    assert calls and {call[2] for call in calls} == {FINALIZED}
    assert ("Keys", (NETUID, 1), FINALIZED) in calls  # vector hotkey check
    assert ("LastUpdate", (NETUID,), FINALIZED) in calls
    assert ("LastUpdate", (NETUID + 4096,), FINALIZED) in calls
    assert [call["mechid"] for call in submit.calls] == [0]
    assert submit.calls[0]["version_key"] == 11
    assert submit.calls[0]["commit_reveal"] is False

    report = finalized_report(chain)
    assert report["mechanism_count"] == 2
    assert report["mechanisms"] == [
        {"mechid": 0, "last_update": 0, "last_update_defaulted": True,
         "blocks_since_last_update": 10, "rate_limit_allows_weights": True},
        {"mechid": 1, "last_update": 1, "last_update_defaulted": False,
         "blocks_since_last_update": 9, "rate_limit_allows_weights": False},
    ]
    assert {call[2] for call in chain.rpc_calls + chain.query_calls} == {FINALIZED}


def test_direct_runtime_reads_take_fresh_finalized_snapshots():
    chain = FinalizedStorage()
    reader = SubstrateReader(chain)
    first = reader.read(NETUID, SIGNER, 0)
    second_hash = "0x" + "ef" * 32
    chain.head, chain.head_hash = 11, second_hash
    chain.overrides["WeightsSetRateLimit"] = 1234
    second = reader.read(NETUID, SIGNER, 0)
    assert first.head == 10 and second.head == 11
    assert first.rate_limit == 1000 and second.rate_limit == 1234
    assert chain.head_reads == {"finalized": 2, "unfinalized": 0}
    assert {call[2] for call in chain.rpc_calls} == {FINALIZED, second_hash}


def test_runtime_tick_refreshes_snapshot_and_mechanism_discovery_next_pass():
    chain = FinalizedStorage()
    chain.overrides["MechanismCountCurrent"] = 2
    chain.rows = {NETUID: [], NETUID + 4096: []}
    loop, submit = make_loop(SubstrateReader(chain))
    assert [result.status for result in loop.tick()] == ["confirmed", "confirmed"]
    assert chain.head_reads == {"finalized": 1, "unfinalized": 0}
    first_call_count = len(chain.rpc_calls)

    chain.head, chain.head_hash = 11, "0x" + "ef" * 32
    chain.overrides["MechanismCountCurrent"] = 3
    chain.overrides["WeightsVersionKey"] = 12
    assert [result.status for result in loop.tick()] == ["confirmed"] * 3
    assert chain.head_reads == {"finalized": 2, "unfinalized": 0}
    assert {call[2] for call in chain.rpc_calls[first_call_count:]} == {chain.head_hash}
    assert [call["version_key"] for call in submit.calls] == [11, 11, 12, 12, 12]


@pytest.mark.parametrize("failure", ["head-rpc", "number-rpc", "bad-hash", "bad-number"])
def test_finalized_snapshot_failure_never_falls_back_to_unfinalized_state(failure):
    chain = FinalizedStorage()

    def unavailable(*args):
        raise OSError("offline finalized-head failure")

    if failure == "head-rpc":
        chain.get_chain_finalised_head = unavailable
    elif failure == "number-rpc":
        chain.get_block_number = unavailable
    elif failure == "bad-hash":
        chain.get_chain_finalised_head = lambda: None
    else:
        chain.get_block_number = lambda block_hash: True
    loop, submit = make_loop(SubstrateReader(chain))
    assert loop.tick()[0].status == "error"
    assert submit.calls == []
    assert chain.head_reads["unfinalized"] == 0
    assert chain.rpc_calls == []


@pytest.mark.parametrize("last,head,rate,allowed", [
    (0, 10, 1000, True), (10, 109, 100, False),
    (10, 110, 100, True), (10, 111, 100, True), (10, 10, 0, True),
])
def test_preflight_and_runtime_report_identical_rate_boundary(last, head, rate, allowed):
    chain = FinalizedStorage()
    chain.head = head
    chain.rows[NETUID] = [0, 0, last]
    chain.overrides["WeightsSetRateLimit"] = rate
    [mechanism] = finalized_report(chain)["mechanisms"]
    assert mechanism == {
        "mechid": 0, "last_update": last, "last_update_defaulted": False,
        "blocks_since_last_update": head - last, "rate_limit_allows_weights": allowed,
    }
    loop, submit = make_loop(SubstrateReader(chain))
    assert loop.tick()[0].status == ("confirmed" if allowed else "not_due")
    assert len(submit.calls) == int(allowed)


def test_complete_preflight_pins_registration_and_every_mechanism_to_one_head():
    chain = FinalizedStorage()
    chain.overrides["MechanismCountCurrent"] = 2
    chain.rows = {NETUID: [], NETUID + 4096: [0]}
    env = SimpleNamespace(
        netuid=NETUID, bundle_role="slave", master_validator_hotkey_ss58=MASTER,
        allocation_expected_genesis_hash=chain.get_block_hash(0),
    )
    preflight.guard_submissions(chain)
    report = preflight.run_checks(env, chain, SIGNER)
    assert report["finalized_hash"] == FINALIZED
    assert report["finalized_block"] == 10
    assert report["all_reads_at_finalized_hash"] is True
    assert [row["last_update"] for row in report["mechanisms"]] == [0, 0]
    assert all(row["rate_limit_allows_weights"] for row in report["mechanisms"])
    assert chain.head_reads == {"finalized": 1, "unfinalized": 0}
    assert {call[2] for call in chain.query_calls + chain.key_calls + chain.rpc_calls} == {FINALIZED}
    assert chain.submissions == []


class DurableStorage(FinalizedStorage):
    """No-network application simulator behind the real durable submitter."""

    def __init__(self):
        super().__init__()
        self.rows[NETUID] = []
        self.weight_rows = {}
        self.events = {}
        self.commits = {}
        self.mode = "apply"

    def value(self, name, params, block_hash):
        if name == "Weights":
            return self.weight_rows.get(params[0], [])
        return super().value(name, params, block_hash)

    def query(self, *, module, storage_function, params, block_hash=None):
        if (module, storage_function) == ("System", "Events"):
            self.query_calls.append((storage_function, tuple(params), block_hash))
            return SimpleNamespace(value=self.events.get(block_hash, []))
        if storage_function == "TimelockedWeightCommits":
            self.query_calls.append((storage_function, tuple(params), block_hash))
            return SimpleNamespace(value=self.commits.get(tuple(params), []))
        return super().query(module=module, storage_function=storage_function,
                             params=params, block_hash=block_hash)

    def query_map(self, *, module, storage_function, params, block_hash):
        assert (module, storage_function) == ("SubtensorModule", "TimelockedWeightCommits")
        return [(epoch, entries) for (index, epoch), entries in self.commits.items() if index == params[0]]

    def submit_extrinsic(self, call, **kwargs):
        # This is a Python-only fake receipt; no SDK extrinsic or signing exists.
        self.submissions.append(call)
        if self.mode == "uncertain":
            raise TimeoutError("offline fake uncertain receipt")
        self.head += 1
        self.head_hash = "0x" + format(self.head, "064x")
        index = NETUID + call["mechid"] * 4096
        self.rows[index] = [0, 0, self.head]
        self.weight_rows[index] = [(1, 65535)]
        self.events[self.head_hash] = [{
            "module_id": "SubtensorModule", "event_id": "WeightsSet", "attributes": [index, 2],
        }]
        return SimpleNamespace(is_success=True, finalized=True, error_message=None)

    def send(self, **kwargs):
        receipt = self.substrate.submit_extrinsic(kwargs)
        return SimpleNamespace(success=receipt.is_success, message="offline fake application")


def durable_loop(chain, state_dir):
    from weight_loop_dispatch import DurableWeightSubmitter

    submit = DurableWeightSubmitter(
        chain, SIGNER, NETUID, str(state_dir), chain.get_block_hash(0), chain.send,
    )
    loop, _ = make_loop(SubstrateReader(chain), submit)
    return loop, submit


@pytest.mark.parametrize("row", [[], [0], [0, 0, 0]])
def test_real_durable_adapter_allows_never_set_first_use_after_cadence_gate(tmp_path, row):
    chain = DurableStorage()
    chain.rows[NETUID] = row
    loop, submit = durable_loop(chain, tmp_path)
    result = loop.tick()[0]
    assert result.status == "confirmed", result
    assert len(chain.submissions) == 1
    assert chain.submissions[0]["uids"] == [1]
    assert submit.reconcile(0) is None


def test_real_durable_adapter_preserves_uncertain_zero_fence_across_restart(tmp_path):
    chain = DurableStorage()
    chain.mode = "uncertain"
    first, _ = durable_loop(chain, tmp_path)
    assert first.tick()[0].status == "waiting"
    assert chain.rows[NETUID] == []
    restarted, _ = durable_loop(chain, tmp_path)
    assert restarted.tick()[0].status == "waiting"
    assert len(chain.submissions) == 1


def test_real_durable_adapter_keeps_preexisting_pending_commit_fenced_at_zero(tmp_path):
    chain = DurableStorage()
    chain.commits[(NETUID, 7)] = [(SIGNER, 5, "0xab", 1)]
    for _ in range(2):
        restarted, _ = durable_loop(chain, tmp_path)
        assert restarted.tick()[0].status == "pending_reveal"
    assert chain.rows[NETUID] == []
    assert chain.submissions == []


def real_sdk_storage(response):
    """Installed SDK StorageKey/decoder with only metadata/RPC transport faked."""
    from async_substrate_interface.utils.storage import StorageKey
    from scalecodec.base import RuntimeConfigurationObject

    key = StorageKey(
        pallet="SubtensorModule", storage_function="LastUpdate", params=[NETUID],
        data=b"\x42" * 32, value_scale_type="Vec<u64>", metadata=None,
        runtime_config=RuntimeConfigurationObject(),
    )
    key.metadata_storage_function = SimpleNamespace(
        value={"modifier": "Default"},
        value_object={"default": SimpleNamespace(value_object=b"\x00")},
    )
    calls = []

    def create_storage_key(*, pallet, storage_function, params, block_hash):
        assert (pallet, storage_function, params, block_hash) == (
            "SubtensorModule", "LastUpdate", [NETUID], FINALIZED,
        )
        return key

    def rpc_request(method, params):
        assert method == "state_getStorageAt"
        assert params == [key.to_hex(), FINALIZED]
        calls.append((method, params))
        return response

    return SimpleNamespace(create_storage_key=create_storage_key, rpc_request=rpc_request), calls


@pytest.mark.parametrize("raw,expected", [
    (None, []), ("0x00", []),
    ("0x0801000000000000000200000000000000", [1, 2]),
])
def test_actual_installed_sdk_decodes_verified_default_and_nonempty_u64_vector(raw, expected):
    from chain_cadence import last_update_for_uid, read_storage

    substrate, calls = real_sdk_storage({"jsonrpc": "2.0", "id": 1, "result": raw})
    decoded = read_storage(substrate, "LastUpdate", [NETUID], FINALIZED)
    assert decoded == expected
    assert last_update_for_uid(decoded, uid=2, head_block=10) == 0
    assert len(calls) == 1


@pytest.mark.parametrize("raw", ["0x", "0x04", "0x0000", "0x040100000000000000ff"])
def test_actual_installed_sdk_rejects_truncated_and_trailing_scale_bytes(raw):
    from chain_cadence import read_storage

    substrate, calls = real_sdk_storage({"result": raw})
    with pytest.raises(CadenceReadError, match="read or decode failed"):
        read_storage(substrate, "LastUpdate", [NETUID], FINALIZED)
    assert len(calls) == 1


@pytest.mark.parametrize("response", [{}, {"error": {"code": -32000}}, {"error": None, "result": None}])
def test_actual_sdk_default_is_unreachable_without_verified_success_envelope(response):
    from chain_cadence import read_storage

    substrate, _ = real_sdk_storage(response)
    with pytest.raises(CadenceReadError, match="response unverified"):
        read_storage(substrate, "LastUpdate", [NETUID], FINALIZED)
