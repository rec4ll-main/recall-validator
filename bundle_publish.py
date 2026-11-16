"""Master publish orchestration: persist -> IPFS -> pin -> readback -> pointer.

The signed envelope is persisted durably (and reused byte-for-byte on restart) before
any chain/IPFS side effect. An unresolved prior-epoch pointer operation is reconciled
from its own persisted key *before* the publication window is evaluated, so a single
timeout never deadlocks future epochs. The pointer observation is written only from an
actual finalized ``Commitments.CommitmentOf`` read with the exact CID in ``[E, D]``.
A fresh finalized snapshot is compared (identity + native cap, ignoring anchor height)
to the trusted E snapshot before dispatch so churn is detected, not missed. Before the
envelope is persisted or published, its rows go through the same
``bundle_weights.validate_weight_rows`` check consume runs (native max-weight cap on
every row, burn row included); a bundle consume would refuse is never published.

``set_commitment`` runs through the ``bundle_dispatch`` adapter: an attempt that never
reached the submit RPC is NOT_DISPATCHED (re-armable inside the window) and one
finalized with ``ExtrinsicFailed`` is FAILED. Once the exact cutoff D is finalized and
readable without the expected CID, the pointer is FAILED as soon as no transaction for
it can still land (known included, or its mortality deadline finalized); until then it
stays fenced so a late inclusion can never overwrite a later epoch's pointer. A pruned
or unreadable cutoff is never treated as absence; it is EXPIRED only after the
mortality deadline.
"""

from __future__ import annotations

import logging

from allocation_snapshot import AllocationSnapshotError, build_snapshot, snapshot_digest
import bundle_commit_guard
from bundle_chain import ChainError, ChainUnavailableError, read_pointer_cid
from bundle_dispatch import (
    MORTALITY_PERIOD,
    DispatchOutcome,
    dispatch,
    expiry_deadline,
    probe_available,
)
from bundle_envelope import build_envelope
from bundle_rotation import rotation_session
from bundle_ipfs import block_is_pinned, unpin_block, fetch_block
from bundle_ipfs import IpfsError, compute_raw_cid, pin_block, publish_block, read_back_block
from bundle_ipfs import TRANSIENT_RETRY_DELAYS, retry_transient
from bundle_journal_types import (
    DISPATCHABLE_STATES,
    ArtifactBinding,
    JournalConflictError,
    JournalState,
    ObservationRef,
    OperationKind,
    OperationRecord,
)
from bundle_runtime import (
    BundleConfig,
    BundleRuntimeError,
    PublishOutcome,
    _existing_state,
    _guard_genesis,
    _journal_for,
    _publish_key,
)
from bundle_schedule import (
    PUBLICATION_CUTOFF_BLOCKS,
    ScheduleError,
    finalized_head,
    read_schedule,
    same_epoch,
)
from bundle_store import ArtifactStore, ArtifactStoreError, artifact_digest
from bundle_v2 import BundleCodecError, parse_bundle
from bundle_weights import signed_burn_uid, validate_weight_rows
from v3_eligibility import EligibilityScopeError, validate_eligibility_artifact

logger = logging.getLogger(__name__)


def master_publish(subtensor, metagraph, wallet, cfg: BundleConfig, *, fetch=None) -> PublishOutcome:
    _guard_genesis(subtensor, cfg.genesis_hash)
    with rotation_session(cfg) as rotation:
        return _master_publish(subtensor, metagraph, wallet, cfg, fetch=fetch, rotation=rotation)


def _master_publish(subtensor, metagraph, wallet, cfg: BundleConfig, *, fetch=None, rotation=None) -> PublishOutcome:
    """Publish the signed bundle: persist -> IPFS -> pin -> readback -> pointer."""
    _guard_genesis(subtensor, cfg.genesis_hash)
    try:
        head = finalized_head(subtensor)
        schedule = read_schedule(subtensor, cfg.netuid, head=head)
    except ScheduleError as exc:
        return PublishOutcome("error", reason=str(exc))

    wallet_hotkey = wallet.hotkey.ss58_address
    store = ArtifactStore(cfg.state_dir)
    journal = _journal_for(cfg.state_dir)
    try:
        _reconcile_prior_pointer(subtensor, cfg, journal, wallet_hotkey, schedule, rotation=rotation)

        if not schedule.in_publish_window(head.block):
            return PublishOutcome("stale", reason="outside [E, D) publish window")

        envelope = store.load(cfg.netuid, schedule.subnet_epoch_index, schedule.epoch_start_block)
        if envelope is None:
            try:
                built = build_envelope(subtensor, metagraph, wallet, cfg, schedule, fetch=fetch)
            except BundleRuntimeError as exc:
                return PublishOutcome("error", reason=str(exc))
            except Exception as exc:
                return PublishOutcome("error", reason=f"envelope build failed: {exc}")
            if built is None:
                return PublishOutcome("blocked", reason="no scored cohort or allocator not ready")
            envelope = built
        # Refuse a bundle consume would refuse (native max-weight cap on every row,
        # burn row included; miner cap; SDK encode equality) before it is persisted,
        # pinned or pointed to. Applies to a rebuilt and to a previously stored envelope.
        refused = _validate_envelope_rows(subtensor, metagraph, cfg, schedule, envelope)
        if refused is not None:
            return PublishOutcome("blocked", reason=f"bundle refused: {refused}")
        try:
            stored = store.store(cfg.netuid, schedule.subnet_epoch_index, schedule.epoch_start_block, envelope)
        except ArtifactStoreError as exc:
            return PublishOutcome("error", reason=str(exc))

        cid = compute_raw_cid(envelope)
        if stored.digest != artifact_digest(envelope):
            return PublishOutcome("error", reason="artifact digest mismatch after persist")

        key = _publish_key(cfg, wallet_hotkey, schedule)
        existing = _existing_state(journal, key)
        if existing is JournalState.OBSERVED:
            return PublishOutcome("published", cid=cid)
        if existing in _RESOLVABLE:
            return _reconcile_pointer_op(
                subtensor, cfg, journal,
                journal.record(key) or OperationRecord(
                    key=key, state=JournalState.UNKNOWN,
                    intent_block=None, digest=artifact_digest(envelope), cid=cid,
                ), rotation=rotation,
            )
        if existing in (JournalState.FAILED, JournalState.EXPIRED):
            rec = journal.record(key)
            return PublishOutcome(
                existing.value, cid=cid, reason=rec.outcome_ref if rec else ""
            )

        try:
            validate_eligibility_artifact(cfg, envelope)
        except EligibilityScopeError as exc:
            return PublishOutcome("blocked", reason=str(exc))

        # Never spend Commitments space we cannot get back (frozen SubnetEpochIndex).
        if existing not in DISPATCHABLE_STATES:
            guard = bundle_commit_guard.preflight(
                subtensor, cfg.netuid, wallet_hotkey, cid, state_dir=cfg.state_dir,
            )
            if not guard.ok:
                return PublishOutcome("skipped", cid=cid, reason=f"{guard.status}: {guard.reason}")

        def ipfs_publish():
            # Idempotent: the persisted envelope always yields the same CID, and pin
            # and read-back are repeatable. No journal/chain side effect happens here.
            if rotation is not None:
                rotation.prepare(envelope, lambda c: block_is_pinned(cfg.ipfs_api_url, c,
                    allow_plaintext=cfg.ipfs_allow_plaintext, plaintext_hosts=cfg.ipfs_plaintext_hosts))
            published = publish_block(
                cfg.ipfs_api_url,
                envelope,
                allow_plaintext=cfg.ipfs_allow_plaintext,
                plaintext_hosts=cfg.ipfs_plaintext_hosts,
            )
            pin_block(
                cfg.ipfs_api_url,
                published,
                allow_plaintext=cfg.ipfs_allow_plaintext,
                plaintext_hosts=cfg.ipfs_plaintext_hosts,
            )
            read_back_block(
                cfg.ipfs_api_url,
                published,
                envelope,
                allow_plaintext=cfg.ipfs_allow_plaintext,
                plaintext_hosts=cfg.ipfs_plaintext_hosts,
            )

        def log_retry(exc, attempt, delay):
            logger.warning(
                "bundle master publish: transient IPFS failure (attempt %d/%d): %s; retrying in %.0fs",
                attempt, len(TRANSIENT_RETRY_DELAYS) + 1, exc, delay,
            )

        try:
            retry_transient(ipfs_publish, on_retry=log_retry)
        except IpfsError as exc:
            return PublishOutcome("error", reason=f"IPFS failed: {exc}")

        binding = ArtifactBinding(digest=artifact_digest(envelope), cid=cid)
        if existing not in DISPATCHABLE_STATES:
            try:
                journal.prepare(key, binding)
            except JournalConflictError as exc:
                return PublishOutcome("blocked", reason=f"journal conflict: {exc}")

        recheck_head = finalized_head(subtensor)
        try:
            recheck = read_schedule(subtensor, cfg.netuid, head=recheck_head)
        except ScheduleError as exc:
            return PublishOutcome("blocked", reason=str(exc))
        if not same_epoch(schedule, recheck) or not recheck.in_publish_window(recheck_head.block):
            return PublishOutcome("blocked", reason="schedule changed before dispatch")
        try:
            fresh_digest = snapshot_digest(
                build_snapshot(subtensor, metagraph, cfg.netuid, None, cfg.genesis_hash)
            )
            envelope_digest = parse_bundle(envelope).payload.snapshot_digest
        except (AllocationSnapshotError, BundleCodecError) as exc:
            return PublishOutcome("blocked", reason=str(exc))
        if fresh_digest != envelope_digest:
            return PublishOutcome("blocked", reason="mapping/cap changed before dispatch")

        try:
            # Recheck after IPFS I/O, immediately before a NEW dispatch intent.
            validate_eligibility_artifact(cfg, envelope)
        except EligibilityScopeError as exc:
            return PublishOutcome("blocked", reason=str(exc))

        if not probe_available(subtensor):
            return PublishOutcome("blocked", reason="submit probe unavailable; refusing to dispatch")
        try:
            authorized = journal.begin(key, intent_block=recheck_head.block, binding=binding)
        except JournalConflictError as exc:
            return PublishOutcome("blocked", reason=f"journal conflict: {exc}")
        if authorized is None:
            return PublishOutcome("blocked", reason="pointer intent already dispatched")

        def guarded_pointer():
            return subtensor.set_commitment(
                wallet=wallet, netuid=cfg.netuid, data=cid,
                period=MORTALITY_PERIOD,
                wait_for_inclusion=True, wait_for_finalization=True,
            )

        result = dispatch(subtensor, guarded_pointer)
        if result.outcome is DispatchOutcome.NOT_DISPATCHED:
            journal.mark_not_dispatched(key, ObservationRef(value=result.detail))
            return PublishOutcome("not_dispatched", cid=cid, reason=result.detail)
        if result.outcome is DispatchOutcome.FAILED:
            journal.mark_failed(key, ObservationRef(value=result.detail))
            bundle_commit_guard.on_dispatch_failed(subtensor, cfg.netuid, wallet_hotkey, result.detail)
            return PublishOutcome("failed", cid=cid, reason=result.detail)
        _stamp_deadline_after_call(subtensor, journal, key, recheck_head.block)
        try:
            space = bundle_commit_guard.read_commit_space(subtensor, cfg.netuid, wallet_hotkey)
            bundle_commit_guard.record_publish(cfg.state_dir, cfg.netuid, space.epoch_index)
        except Exception:
            pass  # budget bookkeeping is best-effort; never disturbs the journal
        if result.outcome is DispatchOutcome.UNCERTAIN:
            journal.mark_unknown(key)
            return PublishOutcome("unknown", cid=cid, reason=result.detail)

        # A successful submission is PENDING, not OBSERVED: finality requires an exact
        # read at the finalized cutoff D, which has not been reached yet (pre-D).
        journal.observe_pending(key)
        return PublishOutcome("pending", cid=cid)
    finally:
        journal.close()


_RESOLVABLE = (JournalState.INTENT, JournalState.PENDING, JournalState.UNKNOWN)


def _validate_envelope_rows(subtensor, metagraph, cfg: BundleConfig, schedule, envelope: bytes) -> str | None:
    """Run consume's weight-row validation on the signed payload with the E snapshot cap."""
    try:
        snapshot = build_snapshot(
            subtensor, metagraph, cfg.netuid, None, cfg.genesis_hash,
            block=schedule.epoch_start_block,
        )
        payload = parse_bundle(envelope).payload
    except (AllocationSnapshotError, BundleCodecError) as exc:
        return str(exc)
    burn_uid, _ = signed_burn_uid(subtensor, metagraph, cfg.netuid, payload)
    return validate_weight_rows(
        payload.uids, payload.weights_u16, native_cap=snapshot.native_cap, burn_uid=burn_uid,
    )


def _stamp_deadline_after_call(subtensor, journal, key, intent_block) -> None:
    """Best-effort deadline stamp from a finalized head read after the SDK call."""
    try:
        head = finalized_head(subtensor)
    except ScheduleError:
        return
    journal.stamp_expiry_deadline(key, expiry_deadline(head.block, intent_block))


def _deadline(journal, rec, head) -> int:
    if rec.expiry_deadline is not None:
        return rec.expiry_deadline
    return journal.stamp_expiry_deadline(
        rec.key, expiry_deadline(head.block, rec.intent_block)
    )


def _ref(text: str) -> ObservationRef:
    return ObservationRef(value=text.encode("ascii", "replace").decode("ascii")[:400])


def _reconcile_prior_pointer(subtensor, cfg: BundleConfig, journal, wallet_hotkey: str, schedule, rotation=None) -> None:
    """Reconcile unresolved pointer ops from prior epochs (releases the cross-epoch gate)."""
    for rec in journal.unresolved(wallet_hotkey, OperationKind.POINTER_PUBLISH):
        if rec.key.subnet_epoch_index != schedule.subnet_epoch_index:
            _reconcile_pointer_op(subtensor, cfg, journal, rec, rotation=rotation)


def master_reconcile_pointer(subtensor, cfg: BundleConfig, wallet_hotkey: str) -> None:
    _guard_genesis(subtensor, cfg.genesis_hash)
    with rotation_session(cfg) as rotation:
        _master_reconcile_pointer(subtensor, cfg, wallet_hotkey, rotation=rotation)
        if rotation is not None:
            opts = dict(allow_plaintext=cfg.ipfs_allow_plaintext, plaintext_hosts=cfg.ipfs_plaintext_hosts)
            errors = rotation.cleanup(lambda c: block_is_pinned(cfg.ipfs_api_url, c, **opts),
                                      lambda c: unpin_block(cfg.ipfs_api_url, c, **opts),
                                      lambda c: fetch_block(cfg.ipfs_api_url, c, **opts))
            if errors:
                import logging
                logging.getLogger(__name__).warning('IPFS rotation cleanup deferred: %s', errors)


def _master_reconcile_pointer(subtensor, cfg: BundleConfig, wallet_hotkey: str, rotation=None) -> None:
    """Reconcile every unresolved pointer op (prior epochs and the current epoch).

    Called by the master tick once the cutoff is finalized, BEFORE weights consume, so
    the pointer journal reaches OBSERVED (or an honest UNKNOWN) while the exact D is
    still readable and before any history pruning. The genesis guard runs first so a
    wrong chain never opens (and never recovers/mutates) the journal.
    """
    _guard_genesis(subtensor, cfg.genesis_hash)
    journal = _journal_for(cfg.state_dir)
    try:
        for rec in journal.unresolved(wallet_hotkey, OperationKind.POINTER_PUBLISH):
            _reconcile_pointer_op(subtensor, cfg, journal, rec, rotation=rotation)
    finally:
        journal.close()


def _reconcile_pointer_op(subtensor, cfg: BundleConfig, journal, rec, rotation=None) -> PublishOutcome:
    """Read the finalized commitment at the op's own cutoff and require the exact CID.

    A finalized head below the cutoff D is never read (and never marked observed): the
    future-D read would fall back to the live tip and fabricate finality. Once D is
    finalized, the historical cutoff read plus the persisted CID is the only proof. A
    readable cutoff without the CID is FAILED once no transaction for the op can still
    land; a pruned or unreadable cutoff stays UNKNOWN until the mortality deadline is
    finalized and is then EXPIRED, never a blind clear.
    """
    cutoff = rec.key.epoch_start_block + PUBLICATION_CUTOFF_BLOCKS
    try:
        head = finalized_head(subtensor)
    except ScheduleError as exc:
        return PublishOutcome("unknown", reason=f"finalized head unavailable: {exc}")
    if head.block < cutoff:
        if rec.state is JournalState.PENDING:
            return PublishOutcome("pending", cid=rec.cid)
        return PublishOutcome("unknown", reason="cutoff not finalized; pointer not provable")
    try:
        observed = read_pointer_cid(
            subtensor, cfg.netuid, cfg.master_hotkey,
            epoch_start_block=rec.key.epoch_start_block,
            cutoff_block=cutoff,
            block=cutoff,
        )
    except ChainUnavailableError as exc:
        # Pruned/unreadable history is never proof of absence. Once no transaction
        # for this op can still land, the gate is released as EXPIRED.
        deadline = _deadline(journal, rec, head)
        if head.block >= deadline:
            ref = _ref(
                f"expired: cutoff {cutoff} unreadable ({exc}); mortality deadline "
                f"{deadline} finalized"
            )
            journal.mark_expired(rec.key, ref, finalized_block=head.block)
            return PublishOutcome("expired", cid=rec.cid, reason=ref.value)
        if rec.state is not JournalState.PENDING:
            journal.mark_unknown(rec.key)
        return PublishOutcome(
            "unknown",
            reason=f"commitment unreadable: {exc}; fenced until finalized block {deadline}",
        )
    except ChainError as exc:
        # Readable at D but not a pointer for [E, D] (e.g. an older epoch's commitment).
        observed = None
        mismatch = f"commitment at cutoff {cutoff} is not this epoch's pointer ({exc})"
    else:
        mismatch = (
            f"commitment at finalized cutoff {cutoff} "
            + ("absent" if observed is None else "has a different CID")
        )
    if observed == rec.cid:
        if rotation is not None:
            rotation.observed(rec.cid, rec.key.epoch_start_block)
        journal.observe(rec.key, ObservationRef(value=rec.cid))
        return PublishOutcome("published", cid=rec.cid)
    # The finalized cutoff is immutable: this epoch's pointer has failed. Release the
    # gate only when no transaction for it can still land and overwrite a later
    # epoch's pointer: PENDING means it was already included (after D).
    if rec.state is JournalState.PENDING:
        ref = _ref(f"failed: {mismatch}; submission was included after the cutoff")
        journal.mark_failed(rec.key, ref)
        return PublishOutcome("failed", cid=rec.cid, reason=ref.value)
    deadline = _deadline(journal, rec, head)
    if head.block >= deadline:
        ref = _ref(f"failed: {mismatch}; mortality deadline {deadline} finalized")
        journal.mark_failed(rec.key, ref)
        return PublishOutcome("failed", cid=rec.cid, reason=ref.value)
    journal.mark_unknown(rec.key)
    return PublishOutcome(
        "unknown",
        reason=f"{mismatch}; fenced until finalized block {deadline}",
    )
