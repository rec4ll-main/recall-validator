"""Block-clock weight loop: set weights on the chain's own rate rules, not on epochs.

Why: the old master only built a bundle and set weights when it saw the subnet's
``LastMechansimStepBlock`` advance. Chain-side epoch deferral once froze that
marker for >7k blocks while the chain still *allowed* weights, so UID0 went stale.

Rules (subtensor spec 477):

* Every loop re-reads LIVE from chain, at one finalized head, with nothing cached across loops:
  ``WeightsSetRateLimit``, ``Tempo``, ``CommitRevealWeightsEnabled``,
  ``WeightsVersionKey``, ``MechanismCountCurrent``, ``Uids[netuid][hotkey]``,
  ``ValidatorPermit`` and ``LastUpdate[netuid + mechid*4096][uid]``.
* A proven never-set ``LastUpdate == 0`` passes the rate rule. Otherwise
  ``due = blocks_since_last_update >= WeightsSetRateLimit`` (both read live every
  loop; nothing hardcoded). Tempo, the epoch marker, the pointer publish and bundle
  consume are NEVER a gate.
* ``LastEpochBlock`` / ``LastMechansimStepBlock`` are NEVER a gate; the epoch marker is
  read only to alert on a stall (telemetry).
* One independent cadence per mechanism.
* Commit-reveal off -> ``set_weights`` / ``set_mechanism_weights``; on ->
  ``commit_timelocked_mechanism_weights``. ``version_key`` always comes from chain.
* Signed by the HOTKEY only (the SDK weight extrinsics sign with ``hotkey`` and fetch
  the nonce from ``system_accountNextIndex`` / ``account_nextIndex`` for the hotkey).
  One process-wide lock per hotkey serializes submissions so two loops never race a
  nonce. Slaves MUST run their own hotkeys; never share a hotkey between processes.
  A coldkey is never unlocked or touched here.
* Confirm: journal intent before mortal dispatch, wait for finalization, classify
  the broadcast outcome, and prove finalized WeightsSet plus the exact row/identity.
  LastUpdate alone records commit activity as well as direct weights. Uncertain attempts
  and pending reveals remain durably fenced across restart.
  ``SettingWeightsTooFast`` waits out the remaining blocks; transient errors back off,
  at most 3 attempts per LastUpdate window; permission/config errors alert.
* Weights never stop because pointer publish or bundle consume failed (the
  commitment stall: Commitments space frozen with SubnetEpochIndex). No fresh
  verified bundle -> alert ``bundle_failclosed`` and keep SETTING weights from the
  last valid verified bundle (uids re-checked against live chain hotkeys). No valid
  bundle at all -> burn 100% to the proven owner/immune UID. Never uniform.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

from bundle_schedule import PUBLICATION_CUTOFF_BLOCKS, block_clock_slot, finalized_head
from chain_cadence import (CadenceReadError, MECHID_STRIDE, last_update_for_uid,
                           mechanism_count as checked_mechanism_count, mechanism_index,
                           rate_allows_weights, read_storage, uint)

logger = logging.getLogger("recall.weight_loop")

DEFAULT_DUE_MARGIN = 15
MAX_TRANSIENT_ATTEMPTS = 3
FAILCLOSED_KEEP = "keep"
FAILCLOSED_BURN = "burn"

# Chain errors that need an operator, not a retry.
ALERT_ERRORS = (
    "NoValidatorPermit",
    "NotEnoughStake",
    "NotEnoughStakeToSetWeights",
    "NotRegistered",
    "HotKeyNotRegisteredInSubNet",
    "IncorrectWeightVersionKey",
    "CommitRevealEnabled",
    "CommitRevealDisabled",
    "InvalidUid",
    "UidVecContainInvalidOne",
    "UidsLengthExceedUidsInSubNet",
)
TOO_FAST = "SettingWeightsTooFast"


# A new slot's bundle cannot be consumed before the publication cutoff has finalized
# (bundle_schedule.PUBLICATION_CUTOFF_BLOCKS) plus a few blocks for the consume pass.
# Inside that window a weight set that is due waits for the new bundle instead of
# re-setting the previous slot's one; after it, the stale fallback applies as before.
CURRENT_SLOT_CONSUME_ALLOWANCE_BLOCKS = 8

@dataclass(frozen=True, slots=True)
class LiveParams:
    """One loop's chain reads, all at the same head. Never reused across loops."""

    head: int
    rate_limit: int
    tempo: int
    commit_reveal: bool
    version_key: int
    mechanism_count: int
    uid: int | None
    permit: bool
    last_update: int | None
    epoch_marker: int | None = None  # LastMechansimStepBlock; telemetry only


@dataclass(frozen=True, slots=True)
class WeightVector:
    """A verified bundle's deterministic uid -> u16 vector plus its freshness tags."""

    uids: tuple[int, ...]
    weights_u16: tuple[int, ...]
    hotkeys: tuple[str, ...]
    as_of_block: int
    bundle_seq: int
    score_version: int | None = None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    success: bool
    message: str = ""
    status: str | None = None  # durable adapter outcome, already reconciled


@dataclass(frozen=True, slots=True)
class LoopResult:
    mechid: int
    status: str  # submitted|confirmed|pending_reveal|not_due|waiting|failclosed|alert|error|skipped
    reason: str = ""


class ChainReader(Protocol):
    def read(self, netuid: int, hotkey: str, mechid: int) -> LiveParams: ...
    def mechanism_count(self, netuid: int) -> int: ...
    def hotkey_at(self, netuid: int, uid: int) -> str | None: ...
    def last_update(self, netuid: int, mechid: int, uid: int) -> int | None: ...


def due(blocks_since: int, rate_limit: int, tempo: int = 0, margin: int = 0) -> bool:
    """The only cadence gate: live WeightsSetRateLimit vs blocks since live LastUpdate.

    ``tempo``/``margin`` are accepted for call compatibility and deliberately ignored.
    """
    return blocks_since >= max(0, rate_limit)


def blocks_until_due(blocks_since: int, rate_limit: int, tempo: int = 0, margin: int = 0) -> int:
    return max(0, max(0, rate_limit) - blocks_since)


def classify_error(message: str) -> str:
    """'too_fast' | 'alert' | 'transient' from an SDK/extrinsic failure message."""
    text = message or ""
    if TOO_FAST in text:
        return "too_fast"
    for name in ALERT_ERRORS:
        if name in text:
            return "alert"
    return "transient"


_HOTKEY_LOCKS: dict[str, threading.Lock] = {}
_HOTKEY_LOCKS_GUARD = threading.Lock()


def hotkey_lock(hotkey_ss58: str) -> threading.Lock:
    """The single signer lock for a hotkey (one in-flight weights extrinsic)."""
    with _HOTKEY_LOCKS_GUARD:
        return _HOTKEY_LOCKS.setdefault(hotkey_ss58, threading.Lock())


@dataclass
class _MechState:
    window: tuple[int, int] | None = None  # (LastUpdate, head // tempo) the attempts belong to
    attempts: int = 0
    not_before: int = 0  # head block before which we do not try again
    last_as_of: int = -1  # block-clock sequence can decrease when Tempo increases
    last_status: str = ""  # outcome of the last tick for this mechanism
    last_reason: str = ""
    bundle_age_blocks: int = -1  # head - as_of_block of the vector last selected, -1 unknown


@dataclass
class WeightLoop:
    """One per hotkey; ``tick()`` runs one pass over every live mechanism."""

    reader: ChainReader
    submit: Callable[..., SubmitResult]
    weights_source: Callable[[], WeightVector | None]
    netuid: int
    hotkey: str
    margin: int = DEFAULT_DUE_MARGIN
    bundle_grace_blocks: int = 60
    # 0 = legacy (re-set the last valid vector forever); > 0 = above this age the
    # vector is treated as invalid and the loop falls back to the owner burn vector.
    max_stale_blocks: int = 0
    failclosed_policy: str = FAILCLOSED_KEEP
    burn_vector: Callable[[], WeightVector | None] = lambda: None
    alert: Callable[[str, str], None] | None = None
    _mech: dict[int, _MechState] = field(default_factory=dict)
    alert_counts: dict[str, int] = field(default_factory=dict)
    last_alert: tuple[str, str] | None = None

    # -- alerts ---------------------------------------------------------- #
    def _alert(self, code: str, detail: str) -> None:
        logger.error("ALERT weight_loop %s: %s", code, detail)
        self.alert_counts[code] = self.alert_counts.get(code, 0) + 1
        self.last_alert = (code, detail)
        if self.alert is not None:
            self.alert(code, detail)

    # -- status ---------------------------------------------------------- #
    def snapshot(self) -> dict:
        """Operator-facing state of the loop: per-mechanism last outcome, bundle age and
        retry state, plus alert counts. Never chain secrets or weights."""
        mechs = {}
        for mechid, st in sorted(self._mech.items()):
            mechs[str(mechid)] = {
                "last_status": st.last_status,
                "last_reason": st.last_reason,
                "bundle_age_blocks": st.bundle_age_blocks,
                "attempts": st.attempts,
                "not_before": st.not_before,
            }
        stale = sorted(int(m) for m, v in mechs.items() if v["last_status"] == "stale_last_valid")
        return {
            "netuid": self.netuid,
            "hotkey": self.hotkey,
            "mechs": mechs,
            "stale_mechs": stale,
            "alert_counts": dict(sorted(self.alert_counts.items())),
            "last_alert": list(self.last_alert) if self.last_alert else None,
        }

    # -- weights selection ------------------------------------------------ #
    def _select_vector(self, p: LiveParams, st: _MechState, reader=None) -> tuple[WeightVector | None, str, str]:
        """(vector, staleness reason or "", invalid reason or "").

        A verified bundle that is merely stale is still the last VALID one: it is
        returned with a reason so the caller alerts and keeps setting weights. Only a
        missing, future-dated, hotkey-mismatched, or (when ``max_stale_blocks`` > 0)
        over-age bundle is invalid.
        """
        vec = self.weights_source()
        if vec is None:
            return None, "", "no verified bundle"
        age = p.head - vec.as_of_block
        st.bundle_age_blocks = age
        if age < 0:
            return None, "", f"bundle as_of_block {vec.as_of_block} is ahead of head {p.head}"
        bad = self._invalid_uids(vec, reader)
        if bad:
            return None, "", f"bundle uids no longer match chain hotkeys: {bad}"
        if self.max_stale_blocks > 0 and age > self.max_stale_blocks:
            return None, "", f"stale bundle beyond max age: age {age} > max_stale_blocks {self.max_stale_blocks}"
        if age > p.tempo + self.bundle_grace_blocks:
            return vec, f"stale bundle: age {age} > tempo {p.tempo} + grace {self.bundle_grace_blocks}", ""
        current_as_of, _ = block_clock_slot(p.head, p.tempo)
        if vec.as_of_block < current_as_of:
            return vec, f"awaiting current-slot bundle: {vec.as_of_block} < {current_as_of}", ""
        return vec, "", ""

    def _invalid_uids(self, vec: WeightVector, reader=None) -> list[int]:
        reader = reader if reader is not None else self.reader
        bad = []
        for uid, hk in zip(vec.uids, vec.hotkeys):
            if reader.hotkey_at(self.netuid, uid) != hk:
                bad.append(uid)
        return bad

    # -- main pass -------------------------------------------------------- #
    def tick(self) -> list[LoopResult]:
        try:
            snapshot = getattr(self.reader, "snapshot", None)
            reader = snapshot() if callable(snapshot) else self.reader
            count = checked_mechanism_count(reader.mechanism_count(self.netuid))
        except Exception as exc:
            return [LoopResult(0, "error", f"MechanismCountCurrent read failed: {exc}")]
        results = []
        for mechid in range(count):
            r = self._tick_mech(mechid, reader)
            st = self._mech.setdefault(mechid, _MechState())
            st.last_status, st.last_reason = r.status, r.reason
            results.append(r)
        return results

    def _tick_mech(self, mechid: int, reader=None) -> LoopResult:
        reader = reader if reader is not None else self.reader
        st = self._mech.setdefault(mechid, _MechState())
        # Reconcile even when cadence is not due or no fresh bundle is available.
        # A possibly broadcast attempt must survive process restarts and keep its
        # fence until finalized application, definitive failure, or safe expiry.
        reconcile = getattr(self.submit, "reconcile", None)
        if reconcile is not None:
            try:
                pending = reconcile(mechid)
            except Exception as exc:
                return LoopResult(mechid, "error", f"dispatch reconciliation failed: {exc}")
            if pending is not None:
                return LoopResult(mechid, pending.status or "waiting", pending.message)
        try:
            p = reader.read(self.netuid, self.hotkey, mechid)
            uint(p.head, "finalized block")
            uint(p.rate_limit, "WeightsSetRateLimit")
            uint(p.tempo, "Tempo")
            uint(p.version_key, "WeightsVersionKey")
            mechanism_index(self.netuid, mechid, p.mechanism_count)
            if not isinstance(p.permit, bool) or not isinstance(p.commit_reveal, bool):
                raise CadenceReadError("permit and commit-reveal values must be booleans")
            if p.uid is not None:
                uint(p.uid, "signer UID", 65535)
            if p.last_update is not None:
                rate_allows_weights(p.last_update, p.head, p.rate_limit)
        except Exception as exc:
            return LoopResult(mechid, "error", f"live read failed: {exc}")
        if p.tempo <= 0:
            return LoopResult(mechid, "error", "tempo must be positive")
        if p.epoch_marker is not None and p.head - p.epoch_marker > 2 * p.tempo:
            # Telemetry only: never gates weights.
            self._alert("epoch_marker_stall", f"mech {mechid} marker {p.epoch_marker} head {p.head}")
        if p.uid is None:
            self._alert("NotRegistered", f"hotkey {self.hotkey} has no uid on {self.netuid}")
            return LoopResult(mechid, "alert", "NotRegistered")
        if not p.permit:
            self._alert("NoValidatorPermit", f"uid {p.uid} has no validator permit")
            return LoopResult(mechid, "alert", "NoValidatorPermit")
        if p.last_update is None:
            return LoopResult(mechid, "error", "LastUpdate unavailable")
        since = p.head - p.last_update
        if since > 1.5 * p.tempo:
            self._alert("weights_stale", f"mech {mechid} uid {p.uid} blocks_since={since} tempo={p.tempo}")
        if not rate_allows_weights(p.last_update, p.head, p.rate_limit):
            return LoopResult(mechid, "not_due", f"{blocks_until_due(since, p.rate_limit)} blocks")
        if p.head < st.not_before:
            return LoopResult(mechid, "waiting", f"backoff until block {st.not_before}")
        window = (p.last_update, p.head // p.tempo)
        if st.window != window:
            st.window, st.attempts = window, 0
        if st.attempts >= MAX_TRANSIENT_ATTEMPTS:
            return LoopResult(mechid, "waiting", "max attempts for this window")

        try:
            vec, stale, invalid = self._select_vector(p, st, reader)
        except Exception as exc:
            return LoopResult(mechid, "error", f"vector identity read failed: {exc}")
        status = "submitted"
        if vec is not None and stale:
            current_as_of, _ = block_clock_slot(p.head, p.tempo)
            ready_at = current_as_of + PUBLICATION_CUTOFF_BLOCKS + CURRENT_SLOT_CONSUME_ALLOWANCE_BLOCKS
            if current_as_of - p.tempo <= vec.as_of_block < current_as_of and p.head < ready_at:
                return LoopResult(mechid, "waiting", f"current-slot bundle not consumable before block {ready_at}")
        if vec is not None and stale:
            # Never stop setting weights because publish/consume failed: re-set the
            # last valid verified bundle and keep alerting.
            self._alert("bundle_failclosed", f"{stale}; setting last valid bundle seq {vec.bundle_seq}")
            status = "stale_last_valid"
        elif vec is None:
            self._alert("bundle_failclosed", f"{invalid}; burning to owner uid")
            vec = self.burn_vector()
            if vec is None:
                self._alert("burn_unresolved", f"owner burn uid unresolved: {invalid}")
                return LoopResult(mechid, "failclosed", f"burn uid unresolved: {invalid}")
            status = "failclosed_burn"

        total = sum(vec.weights_u16)
        if total <= 0:
            return LoopResult(mechid, "failclosed", "empty vector")
        shares = [w / total for w in vec.weights_u16]
        st.attempts += 1
        with hotkey_lock(self.hotkey):
            try:
                res = self.submit(
                    mechid=mechid, uids=list(vec.uids), weights=shares,
                    hotkeys=list(vec.hotkeys),
                    version_key=p.version_key, commit_reveal=p.commit_reveal,
                )
            except Exception as exc:
                res = SubmitResult(False, f"submit raised: {exc}")
        return self._confirm(mechid, st, p, vec, res, status)

    def _confirm(self, mechid, st, p, vec, res: SubmitResult, status) -> LoopResult:
        if res.status is not None:
            if res.success and status != "failclosed_burn":
                st.last_as_of = max(st.last_as_of, vec.as_of_block)
            outcome = status if res.status == "confirmed" and status != "submitted" else res.status
            return LoopResult(mechid, outcome, res.message)
        if not res.success:
            kind = classify_error(res.message)
            if kind == "too_fast":
                st.attempts -= 1  # not a real attempt
                st.not_before = p.last_update + p.rate_limit + 1
                return LoopResult(mechid, "waiting", f"{TOO_FAST}; retry at {st.not_before}")
            if kind == "alert":
                self._alert("submit_rejected", res.message)
                st.attempts = MAX_TRANSIENT_ATTEMPTS  # no retries this window
                return LoopResult(mechid, "alert", res.message)
            st.not_before = p.head + 2 ** st.attempts
            return LoopResult(mechid, "error", f"transient ({st.attempts}/{MAX_TRANSIENT_ATTEMPTS}): {res.message}")
        if status != "failclosed_burn":
            st.last_as_of = max(st.last_as_of, vec.as_of_block)
        if p.commit_reveal:
            # LastUpdate can advance at commit inclusion. Only a finalized
            # application event plus exact row proves reveal/application.
            return LoopResult(mechid, "pending_reveal", f"commit included; seq {vec.bundle_seq}")
        try:
            after = self.reader.last_update(self.netuid, mechid, p.uid)
        except Exception as exc:
            return LoopResult(mechid, "submitted", f"included; LastUpdate re-read failed: {exc}")
        if after is not None and after > p.last_update:
            return LoopResult(mechid, "confirmed" if status == "submitted" else status, f"LastUpdate {after}")
        st.not_before = p.head + 2 ** st.attempts
        return LoopResult(mechid, "error", "included but LastUpdate did not advance")


# --------------------------------------------------------------------------- #
# Production adapters (bittensor 10.5.0 pinned)
# --------------------------------------------------------------------------- #


def _val(obj):
    return getattr(obj, "value", obj)


class SubstrateReader:
    """Verified raw storage reads, pinned to one finalized snapshot per tick."""

    def __init__(self, subtensor, *, head=None) -> None:
        self._st = subtensor
        self._snapshot_head = head

    def snapshot(self):
        # Return a distinct reader: never retain a previous tick's head on self.
        return SubstrateReader(self._st, head=finalized_head(self._st))

    def _head(self):
        return self._snapshot_head if self._snapshot_head is not None else finalized_head(self._st)

    def _q(self, name, params, block_hash):
        return read_storage(self._st.substrate, name, params, block_hash)

    def mechanism_count(self, netuid: int) -> int:
        head = self._head()
        uint(netuid, "netuid", MECHID_STRIDE - 1)
        return checked_mechanism_count(self._q("MechanismCountCurrent", [netuid], head.hash))

    def hotkey_at(self, netuid: int, uid: int) -> str | None:
        head = self._head()
        uint(uid, "UID", 65535)
        v = self._q("Keys", [netuid, uid], head.hash)
        if v is not None and not isinstance(v, str):
            raise CadenceReadError("Keys unreadable at the finalized block")
        return v or None

    def last_update(self, netuid: int, mechid: int, uid: int) -> int | None:
        head = self._head()
        count = checked_mechanism_count(self._q("MechanismCountCurrent", [netuid], head.hash))
        index = mechanism_index(netuid, mechid, count)
        uint(uid, "signer UID", 65535)
        hotkey = self._q("Keys", [netuid, uid], head.hash)
        if not isinstance(hotkey, str) or not hotkey:
            raise CadenceReadError("signer UID is not registered at the finalized block")
        actual_uid = self._q("Uids", [netuid, hotkey], head.hash)
        uint(actual_uid, "signer UID", 65535)
        if actual_uid != uid:
            raise CadenceReadError("signer UID does not match its hotkey at the finalized block")
        return last_update_for_uid(self._q("LastUpdate", [index], head.hash), uid, head.block)

    def read(self, netuid: int, hotkey: str, mechid: int) -> LiveParams:
        head = self._head()
        bh = head.hash
        count = checked_mechanism_count(self._q("MechanismCountCurrent", [netuid], bh))
        index = mechanism_index(netuid, mechid, count)
        uid = self._q("Uids", [netuid, hotkey], bh)
        permit = False
        last_update = None
        if uid is not None:
            uint(uid, "signer UID", 65535)
            if self._q("Keys", [netuid, uid], bh) != hotkey:
                raise CadenceReadError("signer UID does not match its hotkey at the finalized block")
            permits = self._q("ValidatorPermit", [netuid], bh)
            if (not isinstance(permits, (list, tuple))
                    or any(not isinstance(value, bool) for value in permits)):
                raise CadenceReadError("ValidatorPermit unreadable at the finalized block")
            permit = uid < len(permits) and permits[uid] is True
            last_update = last_update_for_uid(self._q("LastUpdate", [index], bh), uid, head.block)
        marker = self._q("LastMechansimStepBlock", [netuid], bh)
        rate = uint(self._q("WeightsSetRateLimit", [netuid], bh), "WeightsSetRateLimit")
        tempo = uint(self._q("Tempo", [netuid], bh), "Tempo")
        if tempo == 0:
            raise CadenceReadError("Tempo must be positive")
        cr = self._q("CommitRevealWeightsEnabled", [netuid], bh)
        if not isinstance(cr, bool):
            raise CadenceReadError("CommitRevealWeightsEnabled unreadable at the finalized block")
        return LiveParams(
            head=head.block, rate_limit=rate, tempo=tempo, commit_reveal=cr,
            version_key=uint(self._q("WeightsVersionKey", [netuid], bh), "WeightsVersionKey"),
            mechanism_count=count, uid=uid, permit=permit, last_update=last_update,
            epoch_marker=None if marker is None else uint(marker, "LastMechansimStepBlock"),
        )


def sdk_submitter(subtensor, wallet, netuid: int, *, state_dir: str, genesis_hash: str):
    """Route by the LIVE commit-reveal flag; sign with the hotkey only."""
    from bittensor.core.extrinsics.weights import (
        commit_timelocked_weights_extrinsic,
        set_weights_extrinsic,
    )
    from bundle_dispatch import MORTALITY_PERIOD
    from weight_loop_dispatch import DurableWeightSubmitter

    def submit(*, mechid, uids, weights, version_key, commit_reveal, hotkeys=None) -> SubmitResult:
        common = dict(
            subtensor=subtensor, wallet=wallet, netuid=netuid, mechid=mechid,
            uids=uids, weights=weights, version_key=version_key, period=MORTALITY_PERIOD,
            wait_for_inclusion=True, wait_for_finalization=True,
        )
        if commit_reveal:
            r = commit_timelocked_weights_extrinsic(block_time=12.0, **common)
        else:
            r = set_weights_extrinsic(**common)
        return r

    return DurableWeightSubmitter(subtensor, wallet.hotkey.ss58_address, netuid,
                                  state_dir, genesis_hash, submit)


def weight_vector_from_store(state_dir: str, netuid: int) -> WeightVector | None:
    """The newest bundle consume verified (``bundle_store.mark_verified``), or None."""
    from bundle_store import ArtifactStore, artifact_digest, latest_verified
    from bundle_v2 import BundleCodecError, parse_bundle

    ref = latest_verified(state_dir, netuid)
    if ref is None:
        return None
    envelope = ArtifactStore(state_dir).load(netuid, ref.bundle_seq, ref.as_of_block)
    if envelope is None or artifact_digest(envelope) != ref.digest:
        return None
    try:
        p = parse_bundle(envelope).payload
    except BundleCodecError:
        return None
    return WeightVector(
        uids=tuple(p.uids), weights_u16=tuple(p.weights_u16), hotkeys=tuple(p.hotkeys),
        as_of_block=ref.as_of_block, bundle_seq=ref.bundle_seq,
        score_version=getattr(p, "score_version", None),
    )


def owner_burn_vector(subtensor, metagraph, netuid: int) -> WeightVector | None:
    """Fail-closed burn: 100% to the proven owner/immune UID (existing Recall policy)."""
    from owner_burn import resolve_owner_burn_uid

    uid, hotkey = resolve_owner_burn_uid(subtensor, metagraph, netuid)
    if uid is None or hotkey is None:
        return None
    return WeightVector(uids=(uid,), weights_u16=(65535,), hotkeys=(hotkey,), as_of_block=0, bundle_seq=-1)
