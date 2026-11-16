"""Slave (and master post-D) consume orchestration: fetch -> verify -> set weights.

Consumes the pinned bundle once D is finalized, verifies bindings and signature,
reconstructs the exact SDK encoding, checks every row (including the burn row) against
the native max-weight cap and the SDK send preconditions, and sets weights with
``max_attempts=1`` through the ``bundle_dispatch`` adapter. An attempt that never
reached the submit RPC is NOT_DISPATCHED and an attempt finalized with
``ExtrinsicFailed`` is FAILED; both release the cross-epoch gate. A possibly broadcast
attempt stays fenced as UNKNOWN until its mortality deadline is finalized, then becomes
EXPIRED (gate released, the same epoch is never retried) - unless a timelocked commit
for that attempt is still pending reveal (including when it was included in a later
epoch than the journal key). The
verified envelope is persisted durably *before* the intent so a later reconciliation
can reconstruct the expected row without re-fetching (even after history pruning).
Unresolved prior-epoch operations are reconciled from their own persisted key first,
so a single timeout never deadlocks future epochs. An observation is written only from
actual finalized reads: exact encoded row/identity plus a matching post-intent
``WeightsSet`` event (applied), or a provable pending commit across actual native
epoch keys (never labeled applied). LastUpdate alone is not reveal proof,
or - when the prior expected row is no longer provable at head (e.g. archive prune) -
a later epoch whose expected row is already APPLIED (supersede; never blind).
UNKNOWN is never blind-retried.
On a score-version upgrade, a journal-bound historical envelope whose
score_version this build no longer accepts is still reconciled for
applied/pending/expired/supersede proof (never re-dispatched, never
relabelled as the supported version).
"""

from __future__ import annotations

from allocation_snapshot import AllocationSnapshotError, build_snapshot, snapshot_digest
from bundle_chain import (
    ChainError,
    WeightStatus,
    commit_epoch_candidates,
    read_pointer_cid,
    reconcile_weights,
    wallet_has_pending_commit,
)
from bundle_dispatch import (
    MORTALITY_PERIOD,
    DispatchOutcome,
    dispatch,
    expiry_deadline,
    probe_available,
    weights_send_precondition,
)
from bundle_fetch import fetch_for_schedule, mark_rejected
from bundle_runtime import ROLE_SLAVE
from bundle_ipfs import IpfsError, compute_raw_cid, fetch_block, fetch_gateway
from bundle_journal_types import (
    DISPATCHABLE_STATES,
    ArtifactBinding,
    JournalConflictError,
    JournalState,
    ObservationRef,
    OperationKind,
)
from bundle_runtime import (
    BundleConfig,
    ConsumeOutcome,
    _existing_state,
    _guard_genesis,
    _journal_for,
    _weights_key,
)
from bundle_schedule import ScheduleError, finalized_head, read_schedule, same_epoch
from bundle_store import ArtifactStore, artifact_digest, latest_verified, mark_verified
from bundle_v2 import (
    BundleCodecError,
    ExpectedBindings,
    parse_bundle,
    parse_bundle_for_journal_recovery,
    verify_bundle,
)
from owner_burn import OwnerReadError
from score_version import UnsupportedScoreVersion
from bundle_weights import signed_burn_uid, validate_weight_rows


def slave_consume(subtensor, metagraph, wallet, cfg: BundleConfig) -> ConsumeOutcome:
    """Consume the pinned bundle after D and set weights exactly once."""
    _guard_genesis(subtensor, cfg.genesis_hash)
    try:
        head = finalized_head(subtensor)
        schedule = read_schedule(subtensor, cfg.netuid, head=head)
    except ScheduleError as exc:
        return ConsumeOutcome("error", reason=str(exc))

    wallet_hotkey = wallet.hotkey.ss58_address
    store = ArtifactStore(cfg.state_dir)
    journal = _journal_for(cfg.state_dir)
    try:
        _reconcile_prior_weights(subtensor, wallet_hotkey, cfg, store, journal, schedule)

        if not schedule.cutoff_finalized(head.block):
            return ConsumeOutcome("blocked", reason="D not finalized")

        if cfg.weight_loop_enabled:
            ref = latest_verified(cfg.state_dir, cfg.netuid)
            # Sequence is head // live tempo, so it can decrease after a resize.
            # An equal block with a different sequence is a different schedule
            # identity and must pass full verification before being marked latest.
            if ref is not None and (
                ref.as_of_block > schedule.epoch_start_block
                or (
                    ref.as_of_block == schedule.epoch_start_block
                    and ref.bundle_seq == schedule.subnet_epoch_index
                )
            ):
                return ConsumeOutcome("verified", reason="bundle already verified")

        key = _weights_key(cfg, wallet_hotkey, schedule)
        existing = _existing_state(journal, key)
        if existing is JournalState.OBSERVED:
            return ConsumeOutcome("applied", reason="already observed")
        if existing in _RESOLVABLE:
            rec = journal.record(key)
            if rec is None:
                return ConsumeOutcome("unknown", reason="operation record missing")
            return _reconcile_weights_op(subtensor, wallet_hotkey, cfg, store, journal, rec, schedule=schedule)
        if existing in (JournalState.FAILED, JournalState.EXPIRED):
            # Terminal for this epoch: never retried (FAILED was rejected on-chain;
            # EXPIRED may have been applied). Later epochs are not blocked.
            rec = journal.record(key)
            return ConsumeOutcome(existing.value, reason=rec.outcome_ref if rec else "")

        try:
            cid = read_pointer_cid(
                subtensor, cfg.netuid, cfg.master_hotkey,
                epoch_start_block=schedule.epoch_start_block,
                cutoff_block=schedule.cutoff_block,
                block=schedule.cutoff_block,
            )
        except ChainError as exc:
            return ConsumeOutcome("blocked", reason=f"pointer read failed: {exc}")
        if cid is None:
            return ConsumeOutcome("blocked", reason="no pointer at D")

        if not cfg.ipfs_api_url and not cfg.gateway_urls:
            return ConsumeOutcome("blocked", reason="no IPFS API or gateway to fetch")
        try:
            envelope = (fetch_for_schedule(cfg, cid, schedule, _fetch_envelope, head_block=head.block)
                        if cfg.role == ROLE_SLAVE else _fetch_envelope(cfg, cid))
        except IpfsError as exc:
            return ConsumeOutcome("blocked", reason=f"bundle fetch failed: {exc}")

        try:
            snapshot = build_snapshot(
                subtensor, metagraph, cfg.netuid, None, cfg.genesis_hash,
                block=schedule.epoch_start_block,
            )
        except AllocationSnapshotError as exc:
            return ConsumeOutcome("blocked", reason=str(exc))
        digest = snapshot_digest(snapshot)

        try:
            bundle = parse_bundle(envelope)
        except BundleCodecError as exc:
            return _rejected(cfg, cid, schedule, f"bundle parse failed: {exc}")

        # Resolve the owner before any row check. A failed owner read proves nothing
        # about the burn row, so it blocks without rejecting: a later poll retries.
        try:
            burn_uid, burn_hotkey = _signed_burn_uid(subtensor, metagraph, cfg, bundle.payload)
        except OwnerReadError:
            return ConsumeOutcome("blocked", reason="owner read failed; retrying")
        winners = {uid: hotkey for uid, hotkey, _, _ in snapshot.cohort}
        for uid, hotkey in zip(bundle.payload.uids, bundle.payload.hotkeys):
            if uid in winners and winners[uid] == hotkey:
                continue
            # Allow the signed owner/immune burn row (v3 units not paid to a miner).
            if (
                burn_uid is not None
                and uid == burn_uid
                and burn_hotkey is not None
                and hotkey == burn_hotkey
            ):
                continue
            return _rejected(cfg, cid, schedule, "payload uid is not a chain-dedup winner")

        expected = ExpectedBindings(
            genesis_hash=cfg.genesis_hash,
            netuid=cfg.netuid,
            master_hotkey=cfg.master_hotkey,
            epoch_start_block=schedule.epoch_start_block,
            subnet_epoch_index=schedule.subnet_epoch_index,
            selection_block=schedule.cutoff_block,
            anchor_block=snapshot.anchor_block,
            anchor_hash=snapshot.anchor_hash,
            snapshot_digest=digest,
            cohort=tuple(zip(bundle.payload.uids, bundle.payload.hotkeys)),
        )
        try:
            bundle = verify_bundle(envelope, expected=expected)
        except BundleCodecError as exc:
            return _rejected(cfg, cid, schedule, f"bundle verification failed: {exc}")

        p = bundle.payload
        # Same validation the master runs before publishing (bundle_weights): native
        # max-weight cap on every row (burn row included, exact chain arithmetic),
        # miner cap on non-burn rows, and SDK encode equality.
        refused = validate_weight_rows(
            p.uids, p.weights_u16, native_cap=snapshot.native_cap, burn_uid=burn_uid,
        )
        if refused is not None:
            return _rejected(cfg, cid, schedule, refused)
        total = sum(p.weights_u16)
        shares = [v / total for v in p.weights_u16]

        # Secondary skip-next policy: discard a fetch crossing publication slots.
        # Masters retain their baseline fetch/retry and schedule behavior.
        if cfg.role == ROLE_SLAVE:
            try:
                current = read_schedule(subtensor, cfg.netuid, head=finalized_head(subtensor))
            except ScheduleError as exc:
                return ConsumeOutcome("blocked", reason=str(exc))
            if not same_epoch(schedule, current):
                return ConsumeOutcome("blocked", reason="schedule changed during bundle fetch")
        store.store(cfg.netuid, schedule.subnet_epoch_index, schedule.epoch_start_block, envelope)
        if cfg.weight_loop_enabled:
            # Decoupled mode: the weight loop submits on its own live-read cadence.
            mark_verified(
                cfg.state_dir, cfg.netuid, schedule.epoch_start_block,
                schedule.subnet_epoch_index, envelope,
            )
            return ConsumeOutcome("verified", reason="bundle verified for weight loop")

        binding = ArtifactBinding(digest=artifact_digest(envelope), cid=cid)
        if existing not in DISPATCHABLE_STATES:
            try:
                journal.prepare(key, binding)
            except JournalConflictError as exc:
                return ConsumeOutcome("blocked", reason=f"journal conflict: {exc}")

        recheck_head = finalized_head(subtensor)
        try:
            recheck = read_schedule(subtensor, cfg.netuid, head=recheck_head)
        except ScheduleError as exc:
            return ConsumeOutcome("blocked", reason=str(exc))
        if not same_epoch(schedule, recheck):
            return ConsumeOutcome("blocked", reason="schedule changed before dispatch")
        try:
            fresh_digest = snapshot_digest(
                build_snapshot(subtensor, metagraph, cfg.netuid, None, cfg.genesis_hash)
            )
        except AllocationSnapshotError as exc:
            return ConsumeOutcome("blocked", reason=str(exc))
        if fresh_digest != digest:
            return ConsumeOutcome("blocked", reason="mapping/cap changed before dispatch")

        # SDK 10.5.0 returns success=False without broadcasting inside the weights
        # rate-limit window or for an unregistered hotkey; check before INTENT.
        blocked = weights_send_precondition(subtensor, wallet_hotkey, cfg.netuid)
        if blocked is not None:
            return ConsumeOutcome("blocked", reason=blocked)
        if not probe_available(subtensor):
            return ConsumeOutcome("blocked", reason="submit probe unavailable; refusing to dispatch")
        try:
            authorized = journal.begin(key, intent_block=recheck_head.block, binding=binding)
        except JournalConflictError as exc:
            return ConsumeOutcome("blocked", reason=f"journal conflict: {exc}")
        if authorized is None:
            return ConsumeOutcome("blocked", reason="weights intent already dispatched")

        def guarded_send():
            return subtensor.set_weights(
                wallet=wallet, netuid=cfg.netuid, uids=list(p.uids), weights=shares,
                max_attempts=1, period=MORTALITY_PERIOD,
                wait_for_inclusion=True, wait_for_finalization=True,
            )
        result = dispatch(subtensor, guarded_send)
        if result.outcome is DispatchOutcome.NOT_DISPATCHED:
            journal.mark_not_dispatched(key, ObservationRef(value=result.detail))
            return ConsumeOutcome("not_dispatched", reason=result.detail)
        if result.outcome is DispatchOutcome.FAILED:
            journal.mark_failed(key, ObservationRef(value=result.detail))
            return ConsumeOutcome("failed", reason=result.detail)
        _stamp_deadline_after_call(subtensor, journal, key, recheck_head.block)
        if result.outcome is DispatchOutcome.UNCERTAIN:
            journal.mark_unknown(key)
            return ConsumeOutcome("unknown", reason=result.detail)

        rec = journal.record(key)
        if rec is None:
            return ConsumeOutcome("unknown", reason="operation record missing")
        return _reconcile_weights_op(
            subtensor, wallet_hotkey, cfg, store, journal, rec, schedule=schedule,
        )
    finally:
        journal.close()


_RESOLVABLE = (JournalState.INTENT, JournalState.PENDING, JournalState.UNKNOWN)


def _rejected(cfg, cid, schedule, reason: str) -> ConsumeOutcome:
    """Fetched bytes failed verification: a secondary closes the publication for good.

    Retries cover fetch failures only; rejected bytes are never fetched again. If the
    rejection cannot be recorded the outcome is still blocked, and any later fetch
    re-runs every check.
    """
    if cfg.role == ROLE_SLAVE:
        try:
            mark_rejected(cfg, cid, schedule)
        except IpfsError as exc:
            reason = f"{reason}; {exc}"
    return ConsumeOutcome("blocked", reason=reason)


def _stamp_deadline_after_call(subtensor, journal, key, intent_block) -> None:
    """Stamp the mortality deadline from a finalized head read after the SDK call.

    Best effort: when the head is unreadable now, the next reconcile stamps it (any
    later finalized head is also a valid upper bound for the signing block).
    """
    try:
        head = finalized_head(subtensor)
    except ScheduleError:
        return
    journal.stamp_expiry_deadline(key, expiry_deadline(head.block, intent_block))


def _settle_unresolved(journal, rec, head, reason: str) -> ConsumeOutcome:
    """Fence a non-provable op until its mortality deadline is finalized, then EXPIRE.

    The deadline is stamped on first sight from the current finalized head; the
    attempt that might have broadcast has already returned (or its process ended), so
    its signing block is at or below this head. Past the deadline no *new* transaction
    for the op can be included. Callers must still confirm there is no pending
    timelocked commit (possibly under a later inclusion epoch) before invoking this:
    extrinsic expiry does not cancel reveal of an already-included commit. The
    application itself stays unproven, so EXPIRED is never retried for its epoch.
    """
    deadline = rec.expiry_deadline
    if deadline is None:
        deadline = journal.stamp_expiry_deadline(
            rec.key, expiry_deadline(head.block, rec.intent_block)
        )
    if head.block >= deadline:
        ref = _bounded_ref(
            f"expired: mortality deadline {deadline} finalized (head {head.block}); {reason}"
        )
        journal.mark_expired(rec.key, ObservationRef(value=ref), finalized_block=head.block)
        return ConsumeOutcome("expired", reason=ref)
    journal.mark_unknown(rec.key)
    return ConsumeOutcome(
        "unknown", reason=f"{reason}; fenced until finalized block {deadline}"
    )


def _bounded_ref(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")[:400]


def _fetch_envelope(cfg: BundleConfig, cid: str) -> bytes:
    """Secondary reads use public gateways only; masters may use Kubo.

    Gateway data still passes CID, signature and publication binding checks.
    Admin URLs are never a secondary fallback, including during reconciliation.
    """
    if cfg.role == ROLE_SLAVE:
        from environment import _reject_lan_bundle_fetch_url
        if not cfg.gateway_urls:
            raise IpfsError("secondary requires public BUNDLE_GATEWAY_URLS")
        try:
            for url in cfg.gateway_urls:
                _reject_lan_bundle_fetch_url(url)
        except ValueError as exc:
            raise IpfsError("invalid secondary public gateway configuration") from exc
        return fetch_gateway(
            cfg.gateway_urls, cid,
            allow_plaintext=cfg.ipfs_allow_plaintext,
            plaintext_hosts=cfg.ipfs_plaintext_hosts,
        )
    errors: list[str] = []
    if cfg.ipfs_api_url:
        try:
            return fetch_block(
                cfg.ipfs_api_url,
                cid,
                allow_plaintext=cfg.ipfs_allow_plaintext,
                plaintext_hosts=cfg.ipfs_plaintext_hosts,
            )
        except IpfsError as exc:
            errors.append(f"kubo: {exc}")
    if cfg.gateway_urls:
        try:
            return fetch_gateway(
                cfg.gateway_urls,
                cid,
                allow_plaintext=cfg.ipfs_allow_plaintext,
                plaintext_hosts=cfg.ipfs_plaintext_hosts,
            )
        except IpfsError as exc:
            errors.append(f"gateways: {exc}")
    if not errors:
        raise IpfsError("no IPFS API or gateway to fetch")
    raise IpfsError("; ".join(errors))


def _reconcile_prior_weights(subtensor, wallet_hotkey, cfg, store, journal, schedule) -> None:
    """Reconcile unresolved weights ops from prior epochs (releases the cross-epoch gate)."""
    for rec in journal.unresolved(wallet_hotkey, OperationKind.WEIGHTS_SUBMIT):
        if rec.key.subnet_epoch_index != schedule.subnet_epoch_index:
            _reconcile_weights_op(
                subtensor, wallet_hotkey, cfg, store, journal, rec, schedule=schedule
            )


def _later_applied_proof(subtensor, wallet_hotkey, cfg, store, schedule, head):
    """Return (later_cid, later_epoch) when the *current* epoch expected row is APPLIED.

    Used only to supersede a prior-epoch UNKNOWN that can no longer be proven at head
    (weights moved on; archive may be pruned). Never observes without this proof.
    """
    later_env = store.load(
        cfg.netuid, schedule.subnet_epoch_index, schedule.epoch_start_block
    )
    later_cid = None
    if later_env is None:
        try:
            later_cid = read_pointer_cid(
                subtensor,
                cfg.netuid,
                cfg.master_hotkey,
                epoch_start_block=schedule.epoch_start_block,
                cutoff_block=schedule.cutoff_block,
                block=schedule.cutoff_block,
            )
        except ChainError:
            return None
        if later_cid is None:
            return None
        try:
            later_env = _fetch_envelope(cfg, later_cid)
        except IpfsError:
            return None
        store.store(
            cfg.netuid, schedule.subnet_epoch_index, schedule.epoch_start_block, later_env
        )
    try:
        later_payload = parse_bundle(later_env).payload
    except BundleCodecError:
        return None
    later_row = tuple(zip(later_payload.uids, later_payload.weights_u16))
    later_cid = later_cid or compute_raw_cid(later_env)
    # Bound: prefer D (cutoff) once finalized; else epoch start — matches consume intent floor.
    bound = (
        schedule.cutoff_block
        if schedule.cutoff_finalized(head.block)
        else schedule.epoch_start_block
    )
    try:
        later_status = reconcile_weights(
            subtensor,
            wallet_hotkey,
            cfg.netuid,
            epoch_start_block=schedule.epoch_start_block,
            subnet_epoch_index=schedule.subnet_epoch_index,
            expected_row=later_row,
            intent_block=bound,
            head=head,
        )
    except ChainError:
        return None
    if later_status is not WeightStatus.APPLIED:
        return None
    return later_cid, schedule.subnet_epoch_index



def _payload_from_persisted_envelope(envelope: bytes, rec):
    """Return the payload for weights reconcile, or None when bytes are not journal-bound.

    Strict score_version validation applies first. When that fails solely because the
    envelope's version is unsupported by this build, a journal-digest-authenticated
    historical parse is used so applied/pending/expired settlement can still run.
    Digest mismatch returns None (caller treats like a missing envelope). Other codec
    failures raise BundleCodecError. Never used for new submissions or re-dispatch.
    """
    if rec.digest is not None and artifact_digest(envelope) != rec.digest:
        return None
    try:
        return parse_bundle(envelope).payload
    except BundleCodecError as exc:
        if not isinstance(exc.__cause__, UnsupportedScoreVersion):
            raise
        return parse_bundle_for_journal_recovery(envelope).payload


def _reconcile_weights_op(
    subtensor, wallet_hotkey, cfg, store, journal, rec, *, schedule=None
) -> ConsumeOutcome:
    """Reconcile one unresolved weights op from its own persisted key and expected row.

    When the prior expected row is not provable at head but a later epoch's expected
    row is already APPLIED, observe the prior as superseded (Coord VAL-020 path).

    Timelocked commits are scanned across every epoch the mortal extrinsic could have
    been included in (and through the current schedule epoch when known). A pending
    commit keeps the cross-epoch fence even after the extrinsic mortality deadline.
    """
    envelope = store.load(cfg.netuid, rec.key.subnet_epoch_index, rec.key.epoch_start_block)
    cid = rec.cid
    status = WeightStatus.UNKNOWN
    try:
        head = finalized_head(subtensor)
    except ScheduleError as exc:
        return ConsumeOutcome("unknown", reason=f"finalized head unavailable: {exc}")

    try:
        epochs = commit_epoch_candidates(
            journal_epoch=rec.key.subnet_epoch_index,
            tempo=schedule.tempo if schedule is not None else None,
            current_epoch=schedule.subnet_epoch_index if schedule is not None else None,
        )
    except ChainError as exc:
        return ConsumeOutcome("unknown", reason=f"commit epoch scan failed: {exc}")

    bound = rec.intent_block if rec.intent_block else rec.key.epoch_start_block

    payload = None
    if envelope is not None:
        try:
            payload = _payload_from_persisted_envelope(envelope, rec)
        except BundleCodecError as exc:
            return ConsumeOutcome("unknown", reason=f"persisted envelope unparseable: {exc}")
    if payload is not None:
        expected_row = tuple(zip(payload.uids, payload.weights_u16))
        cid = cid or compute_raw_cid(envelope)
        try:
            status = reconcile_weights(
                subtensor,
                wallet_hotkey,
                cfg.netuid,
                epoch_start_block=rec.key.epoch_start_block,
                subnet_epoch_index=rec.key.subnet_epoch_index,
                expected_row=expected_row,
                intent_block=rec.intent_block or 0,
                head=head,
                commit_epochs=epochs,
            )
        except (ChainError, ScheduleError) as exc:
            return ConsumeOutcome("unknown", reason=f"weights reconcile failed: {exc}")
        if status is WeightStatus.APPLIED:
            journal.observe(rec.key, ObservationRef(value=f"applied:{cid}"))
            return ConsumeOutcome("applied", reason="finalized WeightsSet + exact row observed")
        if status is WeightStatus.PENDING:
            journal.observe_pending(rec.key)
            return ConsumeOutcome("pending", reason="commit pending reveal")
        # Failed set_weights retry can stamp intent_block > LastUpdate; re-check with
        # epoch_start bound (intent_block=0) so an already-applied row is observed.
        # Keep the journal epoch only: this path is an APPLIED proof, not a commit scan.
        if status is WeightStatus.UNKNOWN:
            try:
                status_ep = reconcile_weights(
                    subtensor,
                    wallet_hotkey,
                    cfg.netuid,
                    epoch_start_block=rec.key.epoch_start_block,
                    subnet_epoch_index=rec.key.subnet_epoch_index,
                    expected_row=expected_row,
                    intent_block=0,
                    head=head,
                )
            except ChainError as exc:
                return ConsumeOutcome("unknown", reason=f"weights reconcile failed: {exc}")
            if status_ep is WeightStatus.APPLIED:
                journal.observe(rec.key, ObservationRef(value=f"applied:{cid}"))
                return ConsumeOutcome(
                    "applied",
                    reason="finalized WeightsSet + exact row observed (epoch_start bound)",
                )
            if status_ep is WeightStatus.PENDING:
                journal.observe_pending(rec.key)
                return ConsumeOutcome("pending", reason="commit pending reveal (epoch-bound recovery)")
    else:
        # Envelope missing or not journal-bound: still fence on a readable pending commit.
        try:
            if wallet_has_pending_commit(
                subtensor,
                wallet_hotkey,
                cfg.netuid,
                epochs=epochs,
                bound=0,
                head=head,
            ):
                journal.observe_pending(rec.key)
                return ConsumeOutcome("pending", reason="commit pending reveal")
        except ChainError as exc:
            return ConsumeOutcome("unknown", reason=f"weights reconcile failed: {exc}")

    # Supersede: prior UNKNOWN (or missing envelope) + later epoch already APPLIED.
    if (
        schedule is not None
        and rec.key.subnet_epoch_index < schedule.subnet_epoch_index
        and status is not WeightStatus.PENDING
    ):
        proof = _later_applied_proof(
            subtensor, wallet_hotkey, cfg, store, schedule, head
        )
        if proof is not None:
            later_cid, later_epoch = proof
            ref = f"superseded_by_epoch:{later_epoch}:applied:{later_cid}"
            journal.observe(rec.key, ObservationRef(value=ref))
            return ConsumeOutcome(
                "applied",
                reason=f"superseded by later APPLIED epoch {later_epoch}",
            )

    if payload is None:
        reason = (
            "persisted envelope missing; cannot prove"
            if envelope is None
            else "persisted envelope not journal-bound; cannot prove"
        )
        return _settle_unresolved(journal, rec, head, reason)
    return _settle_unresolved(journal, rec, head, "weights not provable applied/pending")


def _signed_burn_uid(subtensor, metagraph, cfg, payload):
    """Owner/immune burn UID present in the signed payload (see bundle_weights).

    Strict: raises ``OwnerReadError`` when the owner chain reads fail.
    """
    return signed_burn_uid(subtensor, metagraph, cfg.netuid, payload, strict=True)
