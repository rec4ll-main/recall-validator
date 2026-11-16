"""Weight-loop status file and MinerBurned read."""

from __future__ import annotations

import json
import os

import loop_status
from loop_status import _fixed_to_float, read_miner_burned, write_loop_status
from test_weight_loop import FakeChain, make
from weight_loop import LoopResult


def test_snapshot_records_outcome_age_and_alerts():
    chain = FakeChain()
    loop, sub, alerts = make(chain)
    [r] = loop.tick()
    snap = loop.snapshot()
    mech = snap["mechs"]["0"]
    assert mech["last_status"] == r.status == "confirmed"
    assert mech["bundle_age_blocks"] == chain.head % chain.tempo
    assert snap["stale_mechs"] == []
    # The fixture chain is far past its rate limit, so the tick itself raised telemetry
    # alerts; the counts and the last alert track every call through _alert.
    before = dict(snap["alert_counts"])
    assert before == {code: alerts.count(code) for code in set(alerts)}
    loop._alert("bundle_failclosed", "stale bundle: age 9999")
    loop._alert("bundle_failclosed", "stale bundle: age 10359")
    snap = loop.snapshot()
    assert snap["alert_counts"]["bundle_failclosed"] == before.get("bundle_failclosed", 0) + 2
    assert snap["last_alert"] == ["bundle_failclosed", "stale bundle: age 10359"]
    assert alerts[-2:] == ["bundle_failclosed", "bundle_failclosed"]


def test_stale_bundle_is_visible_in_snapshot():
    chain = FakeChain()
    frozen = chain.head - chain.head % chain.tempo - 5 * chain.tempo
    loop, sub, alerts = make(chain, vec=lambda c: __import__("test_weight_loop").vector(frozen))
    [r] = loop.tick()
    assert r.status == "stale_last_valid"
    snap = loop.snapshot()
    assert snap["stale_mechs"] == [0]
    assert snap["mechs"]["0"]["bundle_age_blocks"] == chain.head - frozen
    assert snap["alert_counts"].get("bundle_failclosed") == 1


def test_write_loop_status_is_atomic_json(tmp_path):
    chain = FakeChain()
    loop, _, _ = make(chain)
    results = loop.tick()
    path = write_loop_status(str(tmp_path / "state"), loop, results,
                             {"share": 1.0, "read_at": 1_700_000_000.0, "encoding": "I96F32", "error": None, "stale": False})
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["miner_burned"] == 1.0 and doc["miner_burned_encoding"] == "I96F32"
    assert doc["miner_burned_read_at"] == "2023-11-14T22:13:20+00:00" and doc["miner_burned_error"] is None
    assert doc["miner_burned_stale"] is False
    assert doc["results"] == [{"mechid": r.mechid, "status": r.status, "reason": r.reason} for r in results]
    assert doc["loop"]["netuid"] == loop.netuid and "stale_mechs" in doc["loop"]
    assert doc["written_at"].endswith("+00:00")
    assert not [f for f in os.listdir(tmp_path / "state") if f.endswith(".tmp")]
    # A second write replaces the file in place.
    write_loop_status(str(tmp_path / "state"), loop, [LoopResult(0, "not_due", "3 blocks")], None)
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["results"][0]["status"] == "not_due" and doc["miner_burned"] is None and doc["miner_burned_stale"] is True


def test_fixed_to_float_is_the_i96f32_adapter_not_a_magnitude_guess():
    assert loop_status.MINER_BURNED_ENCODING == "I96F32"
    assert _fixed_to_float({"bits": 2 ** 32}) == 1.0
    assert _fixed_to_float(2 ** 31) == 0.5
    assert _fixed_to_float(1) == 2 ** -32            # small nonzero values decode, not guessed
    assert _fixed_to_float(2 ** 63) == 2 ** 31       # out of range is reported as is
    assert _fixed_to_float(0) == 0.0
    assert _fixed_to_float(0.25) == 0.25
    assert _fixed_to_float(None) is None and _fixed_to_float("x") is None


class _Sub:
    def __init__(self, value=None, exc=None):
        self.calls = 0
        self._value = value
        self._exc = exc

        class _Substrate:
            def query(inner, module, item, params):
                self.calls += 1
                assert (module, item, params) == ("SubtensorModule", "MinerBurned", [31])
                if self._exc:
                    raise self._exc
                return type("R", (), {"value": self._value})()

        self.substrate = _Substrate()


def test_read_miner_burned_caches_and_keeps_freshness_on_failure():
    loop_status._miner_burned_cache = None
    sub = _Sub({"bits": 2 ** 32})
    first = read_miner_burned(sub, 31, now=1000.0)
    assert first["share"] == 1.0 and first["read_at"] == 1000.0 and first["error"] is None and not first["stale"]
    assert read_miner_burned(sub, 31, now=1100.0)["share"] == 1.0 and sub.calls == 1
    assert read_miner_burned(sub, 31, now=1000.0 + 601)["read_at"] == 1601.0 and sub.calls == 2
    # a failing read keeps the last good share but says so
    sub._exc = RuntimeError("rpc down")
    failed = read_miner_burned(sub, 31, now=1601.0 + 601)
    assert failed["share"] == 1.0 and failed["read_at"] == 1601.0 and failed["stale"] and "rpc down" in failed["error"]
    # a failed read is retried on the next call, not cached for ten minutes
    sub._exc = None
    fresh = read_miner_burned(sub, 31, now=1601.0 + 602)
    assert not fresh["stale"] and fresh["read_at"] == 1601.0 + 602
    loop_status._miner_burned_cache = None
    failing = _Sub(exc=RuntimeError("rpc down"))
    never = read_miner_burned(failing, 31, now=5.0)
    assert never["share"] is None and never["stale"] and never["read_at"] is None
    loop_status._miner_burned_cache = None
