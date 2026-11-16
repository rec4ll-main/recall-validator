"""Master relay of on-chain incentive, weights and MinerBurned to the API."""

from __future__ import annotations

import json
from types import SimpleNamespace

import chain_economics as ce


class _Substrate:
    def __init__(self, rows):
        self.rows = rows

    def query(self, module, item, params):
        if item == "Weights":
            return SimpleNamespace(value=self.rows.get(params[1], []))
        if item == "MinerBurned":
            return SimpleNamespace(value=0)
        raise KeyError(item)


class _Subtensor:
    def __init__(self, rows=None):
        self.substrate = _Substrate(rows if rows is not None else {0: [[13, 16502], [14, 16560], [15, 16233], [16, 16240]]})

    def metagraph(self, netuid, lite=True):
        return SimpleNamespace(hotkeys=["5val", "5x", "5m13", "5m14"], uids=[0, 12, 13, 14],
                               I=[0.0, 0.0, 0.2518, 1.7], Tv=[0.97, 0.0, 0.0, 0.0],
                               block=8353843, last_update=[8353830, 8263983, 0, 0])


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_snapshot_shares_and_weights():
    snap = ce.build_snapshot(_Subtensor(), 353, "5val", 0.0)
    assert snap["block"] == 8353843 and snap["weights_last_update"] == 8353830 and snap["miner_burned_share"] == 0.0
    by = {n["uid"]: n for n in snap["neurons"]}
    assert by[13]["incentive"] == 0.2518 and by[14]["incentive"] == 1.0  # clamped to a share
    total = 16502 + 16560 + 16233 + 16240
    assert abs(by[13]["validator_weight"] - 16502 / total) < 1e-12
    assert by[12]["validator_weight"] == 0.0


def test_no_weights_and_unregistered_validator():
    snap = ce.build_snapshot(_Subtensor(rows={}), 353, "5val", None)
    assert all(n["validator_weight"] is None for n in snap["neurons"]) and snap["miner_burned_share"] is None
    assert ce.build_snapshot(_Subtensor(), 353, "5other", 0.0) is None


def test_publish_posts_internal_key_only_payload():
    seen = {}

    def urlopen(req, timeout=None):
        seen["url"], seen["body"], seen["headers"] = req.full_url, json.loads(req.data), dict(req.header_items())
        return _Resp()

    snap = ce.build_snapshot(_Subtensor(), 353, "5val", 0.0)
    assert ce.publish("https://api.example.test", "k", snap, urlopen=urlopen)
    assert seen["url"] == "https://api.example.test/api/validator/chain-economics"
    assert seen["body"]["netuid"] == 353 and len(seen["body"]["neurons"]) == 4
    assert seen["headers"]["X-internal-api-key"] == "k"
    assert not ce.publish("http://api.example.test", "k", snap, urlopen=urlopen)  # remote plaintext refused
    assert not ce.publish("https://api.example.test", "", snap, urlopen=urlopen)


def test_maybe_publish_is_master_only_and_rate_limited(monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "_last_attempt", None)
    monkeypatch.setattr(ce, "publish", lambda *a, **k: calls.append(a) or True)
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="5val"))
    master = SimpleNamespace(bundle_role="master", netuid=353, recall_api_url="https://api.example.test", internal_api_key="k")
    slave = SimpleNamespace(**{**vars(master), "bundle_role": "slave"})
    assert not ce.maybe_publish(_Subtensor(), wallet, slave, now=1000.0)
    assert ce.maybe_publish(_Subtensor(), wallet, master, now=1000.0)
    assert not ce.maybe_publish(_Subtensor(), wallet, master, now=1100.0)
    assert ce.maybe_publish(_Subtensor(), wallet, master, now=1301.0)
    assert len(calls) == 2


def test_snapshot_carries_validator_trust():
    by = {n["uid"]: n for n in ce.build_snapshot(_Subtensor(), 353, "5val", 0.0)["neurons"]}
    assert by[0]["validator_trust"] == 0.97 and by[13]["validator_trust"] == 0.0


def test_snapshot_without_validator_trust_omits_it(monkeypatch):
    sub = _Subtensor()
    plain = sub.metagraph(353)
    del plain.Tv
    monkeypatch.setattr(sub, "metagraph", lambda netuid, lite=True: plain)
    assert all("validator_trust" not in n for n in ce.build_snapshot(sub, 353, "5val", 0.0)["neurons"])
