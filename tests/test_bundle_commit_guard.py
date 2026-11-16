"""Pointer-publish guard: frozen SubnetEpochIndex must not drain Commitments space."""

from types import SimpleNamespace

import pytest

import bundle_commit_guard as g

HK = "5Master"
CID = "bafkreic7537llg6ccb4kcrfeb34onpmktbjvyknvnpvexzu563tqqeramm"


@pytest.fixture(autouse=True)
def capture_guard_alerts(caplog):
    # Importing bittensor in another test can raise third-party loggers to CRITICAL.
    caplog.set_level("ERROR", logger=g.logger.name)


class Sub:
    def __init__(self, **st):
        self.st = dict(head=8199436, index=22665, leb=8179474, tempo=360, max_space=3100,
                       used={"last_epoch": 22665, "used_space": 3100}, cid=None)
        self.st.update(st)

    def get_chain_head(self):
        return "0xh"

    def get_block_number(self, h):
        return self.st["head"]

    def query(self, module, storage_function, params, block_hash=None):
        s = self.st
        v = {
            "SubnetEpochIndex": s["index"], "LastEpochBlock": s["leb"], "Tempo": s["tempo"],
            "MaxSpace": s["max_space"], "UsedSpaceOf": s["used"],
            "CommitmentOf": None if s["cid"] is None else {
                "deposit": 0, "block": 1, "info": {"fields": [{"Raw59": "0x" + s["cid"].encode().hex()}]}},
        }[storage_function]
        return SimpleNamespace(value=v)


def st(**kw):
    return SimpleNamespace(substrate=Sub(**kw))


def test_frozen_index_full_space_skips_and_raises_distinct_alert(tmp_path, caplog):
    r = g.preflight(st(cid="bafkreiold"), 353, HK, CID, state_dir=str(tmp_path))
    assert not r.ok and r.status == "space" and r.space.epoch_frozen
    assert g.ALERT_FROZEN in caplog.text


def test_full_space_with_live_index_is_plain_space_alert(tmp_path, caplog):
    r = g.preflight(st(cid="bafkreiold", leb=8199360), 353, HK, CID, state_dir=str(tmp_path))
    assert r.status == "space" and g.ALERT_SPACE in caplog.text and g.ALERT_FROZEN not in caplog.text


def test_unchanged_cid_is_skipped(tmp_path):
    r = g.preflight(st(cid=CID, used={"last_epoch": 22665, "used_space": 0}), 353, HK, CID,
                    state_dir=str(tmp_path))
    assert r.status == "unchanged"


def test_space_resets_when_index_advances(tmp_path):
    r = g.preflight(st(index=22666), 353, HK, CID, state_dir=str(tmp_path))
    assert r.ok and r.space.used_space == 0


@pytest.mark.parametrize("remaining, expected", [(99, "space"), (100, "ok")])
def test_raw_cid_requires_minimum_100_bytes(tmp_path, remaining, expected):
    assert len(CID.encode("utf-8")) == 59
    sub = st(used={"last_epoch": 22665, "used_space": 3100 - remaining})
    r = g.preflight(sub, 353, HK, CID, state_dir=str(tmp_path))
    assert r.status == expected
    assert r.ok == (expected == "ok")
    if not r.ok:
        assert "+ 100 > MaxSpace 3100" in r.reason


@pytest.mark.parametrize("payload, cost", [("x" * 101, 101), ("é" * 51, 102)])
@pytest.mark.parametrize("remaining_delta", [-1, 0])
def test_payload_over_minimum_charges_its_utf8_bytes(tmp_path, payload, cost, remaining_delta):
    remaining = cost + remaining_delta
    sub = st(used={"last_epoch": 22665, "used_space": 3100 - remaining})
    r = g.preflight(sub, 353, HK, payload, state_dir=str(tmp_path))
    assert r.ok == (remaining_delta == 0)
    assert r.status == ("ok" if remaining_delta == 0 else "space")
    if not r.ok:
        assert f"+ {cost} > MaxSpace 3100" in r.reason


def test_budget_is_per_subnet_epoch_index(tmp_path):
    sub = st(used={"last_epoch": 22665, "used_space": 0})
    assert g.preflight(sub, 353, HK, CID, state_dir=str(tmp_path)).ok
    g.record_publish(str(tmp_path), 353, 22665)
    assert g.preflight(sub, 353, HK, CID + "x", state_dir=str(tmp_path)).status == "budget"
    sub.substrate.st["index"] = 22666
    assert g.preflight(sub, 353, HK, CID + "x", state_dir=str(tmp_path)).ok


def test_dispatch_space_limit_with_frozen_index_alerts(caplog):
    g.on_dispatch_failed(st(), 353, HK, "ExtrinsicFailed: {'name': 'SpaceLimitExceeded'}")
    assert g.ALERT_FROZEN in caplog.text
