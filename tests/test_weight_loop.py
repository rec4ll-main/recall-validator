"""Block-clock weight loop: live reads, no epoch-marker gate, fail-closed bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from bundle_schedule import FinalizedHead, read_schedule
from bundle_store import latest_verified, mark_verified
from weight_loop import (
    FAILCLOSED_BURN,
    LiveParams,
    SubmitResult,
    WeightLoop,
    WeightVector,
    due,
)

NETUID = 353
HK = "5HotkeyMaster"
MINERS = {1: "5Miner1", 2: "5Miner2", 0: "5Owner"}


@dataclass
class FakeChain:
    head: int = 8186928
    rate_limit: int = 100
    tempo: int = 360
    commit_reveal: bool = False
    version_key: int = 0
    mech_count: int = 1
    uids: dict = field(default_factory=lambda: {HK: 0})
    permit: bool = True
    lu: dict = field(default_factory=lambda: {(0, 0): 8179490})
    epoch_marker: int = 8179474  # frozen LastMechansimStepBlock
    keys: dict = field(default_factory=lambda: dict(MINERS))
    reads: int = 0

    # ChainReader
    def mechanism_count(self, netuid):
        return self.mech_count

    def hotkey_at(self, netuid, uid):
        return self.keys.get(uid)

    def last_update(self, netuid, mechid, uid):
        return self.lu.get((mechid, uid))

    def last_update_map(self):
        return self.lu

    def read(self, netuid, hotkey, mechid):
        self.reads += 1
        uid = self.uids.get(hotkey)
        return LiveParams(
            head=self.head, rate_limit=self.rate_limit, tempo=self.tempo,
            commit_reveal=self.commit_reveal, version_key=self.version_key,
            mechanism_count=self.mech_count, uid=uid, permit=self.permit,
            last_update=None if uid is None else self.last_update_map().get((mechid, uid)),
            epoch_marker=self.epoch_marker,
        )


@dataclass
class FakeSubmit:
    chain: FakeChain
    script: list = field(default_factory=list)  # SubmitResult overrides, in order
    calls: list = field(default_factory=list)

    def __call__(self, *, mechid, uids, weights, version_key, commit_reveal, hotkeys):
        assert hotkeys == [self.chain.keys[u] for u in uids]
        self.calls.append(dict(mechid=mechid, uids=uids, weights=weights,
                               version_key=version_key, commit_reveal=commit_reveal))
        if self.script:
            res = self.script.pop(0)
            if not res.success:
                return res
        else:
            res = SubmitResult(True, "ok")
        if not commit_reveal:
            uid = self.chain.uids[HK]
            self.chain.last_update_map()[(mechid, uid)] = self.chain.head
        return res


def vector(as_of, seq=None):
    return WeightVector(uids=(1, 2), weights_u16=(65535, 32768), hotkeys=("5Miner1", "5Miner2"),
                        as_of_block=as_of, bundle_seq=seq if seq is not None else as_of // 360)


def make(chain, vec=lambda c: vector(c.head - c.head % c.tempo), **kw):
    sub = FakeSubmit(chain)
    alerts = []
    loop = WeightLoop(reader=chain, submit=sub, weights_source=lambda: vec(chain) if vec else None,
                      netuid=NETUID, hotkey=HK, alert=lambda c, d: alerts.append(c), **kw)
    return loop, sub, alerts




def test_frozen_epoch_marker_does_not_block_weights():
    chain = FakeChain()  # live 353 numbers: marker frozen since 8179474, LastUpdate 8179490
    loop, sub, alerts = make(chain)
    [r] = loop.tick()
    assert r.status == "confirmed"
    assert len(sub.calls) == 1
    assert "epoch_marker_stall" in alerts  # telemetry only
    assert "weights_stale" in alerts


def test_rate_limit_change_mid_run_is_honored_without_restart():
    chain = FakeChain(tempo=100, rate_limit=100)
    chain.last_update_map()[(0, 0)] = chain.head - 150
    loop, sub, _ = make(chain, margin=0)
    assert loop.tick()[0].status == "confirmed"
    chain.rate_limit = 200  # governance change between loops
    chain.head += 150
    assert loop.tick()[0].status == "not_due"
    chain.head += 50
    assert loop.tick()[0].status == "confirmed"
    assert len(sub.calls) == 2




def test_setting_weights_too_fast_backs_off_remaining_blocks():
    chain = FakeChain()
    loop, sub, _ = make(chain)
    sub.script = [SubmitResult(False, "Subtensor returned `SettingWeightsTooFast(Module)` error")]
    lu = chain.last_update_map()[(0, 0)]
    r = loop.tick()[0]
    assert r.status == "waiting" and str(lu + chain.rate_limit + 1) in r.reason
    if chain.head < lu + chain.rate_limit + 1:
        assert loop.tick()[0].status == "waiting"
    chain.head = max(chain.head, lu + chain.rate_limit + 1)
    assert loop.tick()[0].status == "confirmed"


def test_transient_errors_retry_with_backoff_max_three_per_window():
    chain = FakeChain()
    loop, sub, _ = make(chain)
    sub.script = [SubmitResult(False, "ws timeout")] * 5
    for _ in range(6):
        loop.tick()
        chain.head += 8
    assert len(sub.calls) == 3


@pytest.mark.parametrize("cr", [False, True])
def test_commit_reveal_toggle_routes_live(cr):
    chain = FakeChain(commit_reveal=cr)
    loop, sub, _ = make(chain)
    r = loop.tick()[0]
    assert sub.calls[0]["commit_reveal"] is cr
    assert r.status == ("pending_reveal" if cr else "confirmed")


def test_commit_reveal_flip_between_loops():
    chain = FakeChain()
    loop, sub, _ = make(chain)
    loop.tick()
    chain.commit_reveal = True
    chain.head += 400
    loop.tick()
    assert [c["commit_reveal"] for c in sub.calls] == [False, True]


def test_uid_change_after_reregistration():
    chain = FakeChain()
    loop, sub, alerts = make(chain)
    loop.tick()
    chain.uids[HK] = 7  # deregistered + re-registered -> new uid, fresh LastUpdate row
    chain.last_update_map()[(0, 7)] = chain.head - 400
    chain.head += 1
    assert loop.tick()[0].status == "confirmed"
    assert chain.last_update_map()[(0, 7)] == chain.head
    chain.uids.pop(HK)
    assert loop.tick()[0].status == "alert"
    assert "NotRegistered" in alerts


def test_version_key_read_from_chain():
    chain = FakeChain(version_key=42)
    loop, sub, _ = make(chain)
    loop.tick()
    assert sub.calls[0]["version_key"] == 42


def test_one_loop_per_mechanism():
    chain = FakeChain(mech_count=2)
    chain.last_update_map()[(1, 0)] = chain.head - 10  # mech 1 not due yet
    loop, sub, _ = make(chain)
    results = loop.tick()
    assert [r.status for r in results] == ["confirmed", "not_due"]
    assert [c["mechid"] for c in sub.calls] == [0]


@pytest.mark.parametrize("err", ["NoValidatorPermit", "NotEnoughStakeToSetWeights",
                                 "IncorrectWeightVersionKey", "UidVecContainInvalidOne"])
def test_permission_errors_alert_not_retry(err):
    chain = FakeChain()
    loop, sub, alerts = make(chain)
    sub.script = [SubmitResult(False, err)]
    assert loop.tick()[0].status == "alert"
    chain.head += 5
    loop.tick()
    assert len(sub.calls) == 1 and "submit_rejected" in alerts


def test_no_permit_alerts_before_submit():
    chain = FakeChain(permit=False)
    loop, sub, alerts = make(chain)
    assert loop.tick()[0].status == "alert" and not sub.calls






def test_tempo_increase_accepts_newer_block_with_lower_sequence():
    chain = FakeChain()
    loop, sub, _ = make(chain, vec=lambda c: vector(c.head - 20, c.head // c.tempo))
    assert loop.tick()[0].status == "confirmed"
    chain.tempo *= 2
    chain.head += chain.tempo
    assert loop.tick()[0].status == "confirmed"
    assert len(sub.calls) == 2








def test_bundle_with_changed_hotkey_is_rejected():
    chain = FakeChain()
    chain.keys[2] = "5SomeoneElse"
    loop, sub, _ = make(chain)
    assert loop.tick()[0].status == "failclosed" and not sub.calls


def test_slave_fail_closed_keep_never_uniform():
    chain = FakeChain()
    loop, sub, _ = make(chain, vec=None)
    assert loop.tick()[0].status == "failclosed"
    assert sub.calls == []


def test_slave_fail_closed_burn_to_owner_uid():
    chain = FakeChain()
    burn = WeightVector(uids=(0,), weights_u16=(65535,), hotkeys=("5Owner",), as_of_block=0, bundle_seq=-1)
    loop, sub, _ = make(chain, vec=None, failclosed_policy=FAILCLOSED_BURN, burn_vector=lambda: burn)
    r = loop.tick()[0]
    assert r.status == "failclosed_burn"
    assert sub.calls[0]["uids"] == [0] and sub.calls[0]["weights"] == [1.0]


def test_nothing_cached_across_loops():
    chain = FakeChain()
    loop, _, _ = make(chain)
    loop.tick()
    loop.tick()
    assert chain.reads == 2


def test_schedule_follows_head_clock_when_epoch_marker_frozen():
    frozen = SimpleNamespace(last_epoch_block=8179474, subnet_epoch_index=22720,
                             tempo=360, pending_epoch_at=8179834)
    st = SimpleNamespace(get_epoch_schedule_state=lambda netuid, block=None: frozen)
    s = read_schedule(st, NETUID, head=FinalizedHead(block=8186928, hash="0xab"))
    assert s.epoch_start_block == 8186928 - 8186928 % 360
    assert s.bundle_seq == 8186928 // 360
    assert s.chain_epoch_block == 8179474  # telemetry only
    s2 = read_schedule(st, NETUID, head=FinalizedHead(block=8186928 + 360, hash="0xcd"))
    assert s2.epoch_start_block == s.epoch_start_block + 360


def test_mark_verified_is_monotonic(tmp_path):
    mark_verified(str(tmp_path), NETUID, 720, 2, b"a")
    mark_verified(str(tmp_path), NETUID, 360, 1, b"b")
    ref = latest_verified(str(tmp_path), NETUID)
    assert (ref.as_of_block, ref.bundle_seq) == (720, 2)
    assert latest_verified(str(tmp_path), 31) is None


# --- Commitment stall: weights must not depend on pointer/bundle health --- #

def test_due_formula_is_rate_limit_only():
    assert due(100, 100)
    assert not due(99, 100)
    assert due(100, 100, 360, 15)  # tempo/margin are ignored
    assert due(5, 0)


def test_tempo_change_does_not_gate():
    chain = FakeChain(rate_limit=10, tempo=360)
    loop, sub, _ = make(chain)
    loop.tick()
    chain.head += 9
    assert loop.tick()[0].status == "not_due"
    chain.tempo = 100
    chain.head += 1
    assert loop.tick()[0].status == "confirmed"


def test_frozen_epoch_full_space_sets_weights_at_rate_limit_cadence():
    """SubnetEpochIndex frozen, Commitments space full: no new bundle can ever be
    published, so the only verified bundle ages forever. Weights must still be set
    every WeightsSetRateLimit blocks from that bundle."""
    chain = FakeChain(rate_limit=100)
    last_valid = vector(chain.head - 20)
    loop, sub, alerts = make(chain, vec=lambda c: last_valid)
    statuses = []
    for _ in range(15):  # 15 * 100 blocks ~ the observed 1500-block stall
        statuses.append(loop.tick()[0].status)
        chain.head += 50
        statuses.append(loop.tick()[0].status)  # mid-interval: not due
        chain.head += 50
    # At the first slot boundary the bundle is the previous slot's, so the due set waits
    # once (inside the publication cutoff window) and later sets land a tick later.
    assert statuses.count("waiting") == 1
    assert statuses.count("not_due") == 14
    assert len(sub.calls) == 15
    assert statuses[0] == "confirmed"
    assert {s for s in statuses[8:] if s != "not_due"} == {"stale_last_valid"}  # still setting once stale
    assert all(c["uids"] == [1, 2] for c in sub.calls)
    assert "bundle_failclosed" in alerts and "epoch_marker_stall" in alerts


def test_stale_bundle_sets_from_last_valid():
    chain = FakeChain()
    loop, sub, alerts = make(chain, vec=lambda c: vector(c.head - c.tempo - 61))
    r = loop.tick()[0]
    assert r.status == "stale_last_valid"
    assert sub.calls and sub.calls[0]["uids"] == [1, 2]
    assert "bundle_failclosed" in alerts  # alert kept


def test_older_bundle_after_newer_still_sets_last_valid():
    chain = FakeChain()
    blocks = [chain.head - 20, chain.head - 21]
    loop, sub, _ = make(chain, vec=lambda c: vector(blocks[0]))
    loop.tick()
    blocks.pop(0)
    chain.head += 345
    assert loop.tick()[0].status == "stale_last_valid"
    assert len(sub.calls) == 2


def test_previous_slot_bundle_waits_for_new_one_inside_cutoff_window():
    # 12 blocks into slot 3960: the new bundle cannot be consumable yet, so a due weight
    # set waits instead of re-setting the previous slot's bundle.
    chain = FakeChain(head=3972, lu={(0, 0): 3620})
    loop, sub, alerts = make(chain, vec=lambda c: vector(3600))
    r = loop.tick()[0]
    assert r.status == "waiting" and "3980" in r.reason
    assert not sub.calls and "bundle_failclosed" not in alerts


def test_previous_slot_bundle_reused_after_cutoff_window():
    # Past cutoff + consume allowance with still no new bundle: fall back as before.
    chain = FakeChain(head=3980, lu={(0, 0): 3620})
    loop, sub, alerts = make(chain, vec=lambda c: vector(3600))
    assert loop.tick()[0].status == "stale_last_valid"
    assert len(sub.calls) == 1 and "bundle_failclosed" in alerts


def test_new_slot_bundle_inside_window_sets_normally():
    chain = FakeChain(head=3975, lu={(0, 0): 3620})
    loop, sub, alerts = make(chain, vec=lambda c: vector(3960))
    assert loop.tick()[0].status == "confirmed"
    assert len(sub.calls) == 1 and "bundle_failclosed" not in alerts


def test_older_than_previous_slot_is_not_held_back():
    # A bundle two slots old means publishing is broken: no waiting, fall back at once.
    chain = FakeChain(head=3965, lu={(0, 0): 3620})
    loop, sub, alerts = make(chain, vec=lambda c: vector(3560))
    assert loop.tick()[0].status == "stale_last_valid"
    assert len(sub.calls) == 1


def test_no_valid_bundle_burns_to_owner_regardless_of_keep_policy():
    chain = FakeChain()
    burn = WeightVector((0,), (65535,), ("5Owner",), 0, -1)
    loop, sub, alerts = make(chain, vec=None, burn_vector=lambda: burn)
    assert loop.tick()[0].status == "failclosed_burn"
    assert sub.calls[0]["uids"] == [0] and "bundle_failclosed" in alerts


def test_rate_limit_change_adapts_cadence():
    chain = FakeChain(rate_limit=100)
    loop, sub, _ = make(chain, vec=lambda c: vector(c.head - 20))
    assert loop.tick()[0].status in ("confirmed", "stale_last_valid")
    chain.head += 100
    assert loop.tick()[0].status in ("confirmed", "stale_last_valid")
    chain.rate_limit = 30  # lowered live
    chain.head += 30
    assert loop.tick()[0].status in ("confirmed", "stale_last_valid")
    chain.rate_limit = 250  # raised live
    chain.head += 200
    assert loop.tick()[0].status == "not_due"
    chain.head += 50
    assert loop.tick()[0].status in ("confirmed", "stale_last_valid")
    assert len(sub.calls) == 4


# ----------------------------------------------------------------- max stale age

OWNER_BURN = WeightVector(uids=(0,), weights_u16=(65535,), hotkeys=("5Owner",), as_of_block=0, bundle_seq=-1)


def test_bundle_beyond_max_stale_age_falls_back_to_owner_burn():
    chain = FakeChain()
    loop, sub, alerts = make(chain, vec=lambda c: vector(c.head - 5000),
                             max_stale_blocks=4320, burn_vector=lambda: OWNER_BURN)
    r = loop.tick()[0]
    assert r.status == "failclosed_burn"
    assert sub.calls and sub.calls[0]["uids"] == [0]
    assert "bundle_failclosed" in alerts


def test_bundle_within_max_stale_age_is_still_set_as_last_valid():
    chain = FakeChain()
    loop, sub, alerts = make(chain, vec=lambda c: vector(c.head - c.tempo - 61),
                             max_stale_blocks=4320, burn_vector=lambda: OWNER_BURN)
    r = loop.tick()[0]
    assert r.status == "stale_last_valid"
    assert sub.calls and sub.calls[0]["uids"] == [1, 2]


def test_max_stale_zero_keeps_legacy_stale_forever_behaviour():
    chain = FakeChain()
    loop, sub, _ = make(chain, vec=lambda c: vector(c.head - 5000),
                        max_stale_blocks=0, burn_vector=lambda: OWNER_BURN)
    assert loop.tick()[0].status == "stale_last_valid"
    assert sub.calls[0]["uids"] == [1, 2]
