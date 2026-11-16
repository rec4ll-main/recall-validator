"""Real config/consume/store integration for the decoupled weight-loop input.

Consume tests replace only IPFS transport. Bundles use real signatures and
verification, the on-disk artifact store/journal, and the deterministic chain/SDK
fixture. The full-tick test instruments the loop to observe its verified input.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

import bundle_consume
import main
from bundle_ipfs import IpfsError, compute_raw_cid
from bundle_store import ArtifactStore, artifact_digest, latest_verified, mark_verified
from chain_v2_fake import (
    GENESIS,
    MASTER,
    NETUID,
    SLAVE,
    FakeChain,
    FakeMetagraph,
    FakeSubtensor,
    FakeWallet,
    build_envelope_for,
    config,
    publish_pointer,
)
from environment import EnvironmentVariables
from weight_loop import weight_vector_from_store


class World:
    def __init__(self, tmp_path, monkeypatch, *, tempo=360):
        self.state = tmp_path / "state"
        self.chain = FakeChain(tempo=tempo)
        self.sub = FakeSubtensor(self.chain)
        self.metagraph = FakeMetagraph()
        self.blocks = {}
        self.fetches = []

        def fetch(api_url, cid, **kwargs):
            self.fetches.append(cid)
            data = self.blocks.get(cid)
            if data is None or compute_raw_cid(data) != cid:
                raise IpfsError("IPFS HTTP 404")
            return data

        monkeypatch.setattr(bundle_consume, "fetch_block", fetch)
        monkeypatch.setattr(bundle_consume, "fetch_gateway", fetch)

    def publish(self, as_of, **kwargs):
        self.chain.advance_to(as_of + 5)
        envelope = build_envelope_for(self.chain, self.sub, as_of, **kwargs)
        cid = publish_pointer(self.chain, envelope, as_of + 5)
        self.blocks[cid] = envelope
        self.chain.advance_to(as_of + 20)
        return envelope

    def env(self, *, enabled=None, role="slave"):
        settings = dict(
            bundle_mode="chain-v2",
            bundle_role=role,
            allocation_mode="v2",
            allocation_expected_genesis_hash=GENESIS,
            network="test",
            netuid=NETUID,
            master_validator_hotkey_ss58=MASTER.ss58_address,
            bundle_state_dir=str(self.state),
            bundle_ipfs_api_url="https://ipfs.example.invalid" if role == "master" else "",
        bundle_gateway_urls="https://gateway.example.invalid",
            _env_file=None,
        )
        if enabled is not None:
            settings["weight_loop_enabled"] = enabled
        return EnvironmentVariables(**settings)

    def consume(self):
        cfg = replace(
            config(self.state, ipfs_api_url="https://ipfs.example.invalid"),
            weight_loop_enabled=True,
        )
        return bundle_consume.slave_consume(
            self.sub, self.metagraph, FakeWallet(SLAVE), cfg,
        )

    def weight_operations(self):
        with sqlite3.connect(self.state / "journal.sqlite3") as connection:
            return connection.execute(
                "SELECT state FROM operations WHERE operation_kind = 'weights_submit'"
            ).fetchall()


@pytest.mark.parametrize("role", ["master", "slave"])
def test_enabled_main_consume_persists_verified_bundle_without_legacy_submit(
    tmp_path, monkeypatch, role,
):
    w = World(tmp_path, monkeypatch)
    envelope = w.publish(3600)
    wallet = FakeWallet(MASTER if role == "master" else SLAVE)

    assert main.run_bundle_epoch(w.sub, wallet, w.metagraph, w.env(enabled=True, role=role))

    ref = latest_verified(str(w.state), NETUID)
    assert (ref.as_of_block, ref.bundle_seq, ref.digest) == (3600, 10, artifact_digest(envelope))
    assert ArtifactStore(str(w.state)).load(NETUID, 10, 3600) == envelope
    vector = weight_vector_from_store(str(w.state), NETUID)
    assert (vector.as_of_block, vector.bundle_seq) == (3600, 10)
    assert w.sub.set_weights_calls == 0
    assert w.chain.submissions == []
    assert w.weight_operations() == []

    # A repeated consume of the same identity does not re-fetch or submit.
    assert main.run_bundle_epoch(w.sub, wallet, w.metagraph, w.env(enabled=True, role=role))
    assert len(w.fetches) == 1
    assert w.sub.set_weights_calls == 0


def test_full_tick_default_mode_verifies_before_running_only_weight_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("WEIGHT_LOOP_ENABLED", raising=False)
    monkeypatch.delenv("weight_loop_enabled", raising=False)
    monkeypatch.setattr(main, "_WEIGHT_LOOPS", {})
    w = World(tmp_path, monkeypatch)
    envelope = w.publish(3600)
    env = w.env()
    wallet = FakeWallet(SLAVE)
    assert env.weight_loop_enabled is True
    assert "weight_loop_enabled" not in env.model_fields_set
    events = []

    class InstrumentedLoop:
        def tick(self):
            vector = weight_vector_from_store(str(w.state), NETUID)
            assert vector is not None
            assert (vector.as_of_block, vector.bundle_seq) == (3600, 10)
            assert ArtifactStore(str(w.state)).load(NETUID, 10, 3600) == envelope
            assert w.sub.set_weights_calls == 0
            assert w.weight_operations() == []
            events.append("loop")
            return []

    loop = InstrumentedLoop()

    def build_loop(subtensor, actual_wallet, metagraph, actual_env):
        assert (subtensor, actual_wallet, metagraph, actual_env) == (
            w.sub, wallet, w.metagraph, env,
        )
        assert latest_verified(str(w.state), NETUID).digest == artifact_digest(envelope)
        events.append("build")
        return loop

    monkeypatch.setattr(main, "build_weight_loop", build_loop)
    monkeypatch.setattr(main, "_maybe_refresh_registry", lambda *args: events.append("registry"))

    assert main.run_chain_v2_tick(w.sub, wallet, w.metagraph, env)
    assert events == ["build", "loop", "registry"]
    assert main._WEIGHT_LOOPS == {SLAVE.ss58_address: loop}
    assert w.sub.set_weights_calls == 0
    assert w.chain.submissions == []

    # A second full tick reuses the loop while consume remains idempotent.
    assert main.run_chain_v2_tick(w.sub, wallet, w.metagraph, env)
    assert events == ["build", "loop", "registry", "loop", "registry"]
    assert len(w.fetches) == 1
    assert w.sub.set_weights_calls == 0
    assert w.chain.submissions == []


@pytest.mark.parametrize("role", ["master", "slave"])
def test_disabled_main_consume_preserves_legacy_dispatch(tmp_path, monkeypatch, role):
    w = World(tmp_path, monkeypatch)
    envelope = w.publish(3600)
    wallet = FakeWallet(MASTER if role == "master" else SLAVE)

    assert main.run_bundle_epoch(w.sub, wallet, w.metagraph, w.env(enabled=False, role=role))

    assert latest_verified(str(w.state), NETUID) is None
    assert ArtifactStore(str(w.state)).load(NETUID, 10, 3600) == envelope
    assert w.sub.set_weights_calls == 1
    assert len(w.chain.submissions) == 1
    assert w.weight_operations() == [("observed",)]

    # Legacy exactly-once journal behavior is unchanged.
    assert main.run_bundle_epoch(w.sub, wallet, w.metagraph, w.env(enabled=False, role=role))
    assert w.sub.set_weights_calls == 1


def test_enabled_consume_does_not_mark_invalid_signature(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.publish(3600, signer=SLAVE)

    assert not main.run_bundle_epoch(
        w.sub, FakeWallet(SLAVE), w.metagraph, w.env(enabled=True),
    )

    assert latest_verified(str(w.state), NETUID) is None
    assert ArtifactStore(str(w.state)).load(NETUID, 10, 3600) is None
    assert w.sub.set_weights_calls == 0
    assert w.weight_operations() == []


def test_tempo_increase_consumes_newer_block_despite_lower_sequence(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.publish(3600)
    assert w.consume().status == "verified"
    assert latest_verified(str(w.state), NETUID).bundle_seq == 10

    w.chain.tempo = 720
    envelope = w.publish(4320)
    assert w.consume().status == "verified"

    ref = latest_verified(str(w.state), NETUID)
    assert (ref.as_of_block, ref.bundle_seq, ref.digest) == (4320, 6, artifact_digest(envelope))
    vector = weight_vector_from_store(str(w.state), NETUID)
    assert (vector.as_of_block, vector.bundle_seq) == (4320, 6)
    assert len(w.fetches) == 2
    assert w.sub.set_weights_calls == 0
    assert w.weight_operations() == []


@pytest.mark.parametrize("old_tempo,new_tempo", [(360, 720), (720, 360)])
def test_equal_block_after_tempo_resize_reverifies_schedule_identity(
    tmp_path, monkeypatch, old_tempo, new_tempo,
):
    w = World(tmp_path, monkeypatch, tempo=old_tempo)
    w.publish(3600)
    assert w.consume().status == "verified"
    old_ref = latest_verified(str(w.state), NETUID)

    # E and D stay the same, but the pointer at D still names the old schedule.
    # A cached equal-E reference must not hide the now-invalid sequence binding.
    w.chain.tempo = new_tempo
    outcome = w.consume()

    assert outcome.status == "blocked", outcome
    assert "bundle verification failed" in outcome.reason
    assert "subnet_epoch_index" in outcome.reason
    assert latest_verified(str(w.state), NETUID) == old_ref
    assert len(w.fetches) == 2
    assert w.sub.set_weights_calls == 0


@pytest.mark.parametrize("new_as_of,new_seq", [(4320, 6), (4320, 12)])
def test_verified_reference_uses_block_order(tmp_path, new_as_of, new_seq):
    state = str(tmp_path)
    mark_verified(state, NETUID, 3600, 10, b"old")
    mark_verified(state, NETUID, new_as_of, new_seq, b"new")
    assert latest_verified(state, NETUID).digest == artifact_digest(b"new")

    # A higher sequence is not enough to roll the as-of block backwards.
    mark_verified(state, NETUID, 3960, 100, b"backwards")
    ref = latest_verified(state, NETUID)
    assert (ref.as_of_block, ref.bundle_seq) == (new_as_of, new_seq)
    assert ref.digest == artifact_digest(b"new")


@pytest.mark.parametrize("old_seq,new_seq", [(10, 5), (5, 10)])
def test_verified_reference_accepts_reverified_equal_block_resize(tmp_path, old_seq, new_seq):
    state = str(tmp_path)
    mark_verified(state, NETUID, 3600, old_seq, b"old identity")
    mark_verified(state, NETUID, 3600, new_seq, b"new identity")
    ref = latest_verified(state, NETUID)
    assert (ref.as_of_block, ref.bundle_seq) == (3600, new_seq)
    assert ref.digest == artifact_digest(b"new identity")

    # Repeated verification of the exact same schedule identity is idempotent.
    mark_verified(state, NETUID, 3600, new_seq, b"conflicting bytes")
    assert latest_verified(state, NETUID) == ref
