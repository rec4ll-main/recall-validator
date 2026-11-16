"""Durable, mortal weight-loop dispatch using the existing journal state machine.

Each mechanism has its own journal, so a pending reveal does not gate other
mechanisms. The legacy journal remains a migration/restore fence. Operation keys
use finalized dispatch blocks, never the bundle clock or a predicted chain epoch.
The immutable artifact records the exact SDK-encoded row for restart recovery.
"""

from __future__ import annotations

import json
from pathlib import Path

from allocation_encoding import convert_weights_and_uids
from bundle_chain import ChainError, read_timelocked_commits
from bundle_dispatch import DispatchOutcome, dispatch, expiry_deadline, probe_available
from bundle_journal_types import (
    ArtifactBinding, JournalState, ObservationRef, OperationKey, OperationKind,
)
from bundle_runtime import _guard_genesis, _journal_for
from bundle_schedule import finalized_head
from chain_cadence import (last_update_for_uid, mechanism_count, mechanism_index,
                           read_storage, uint)
from bundle_store import ArtifactStore, artifact_digest
from weight_application_proof import find_application_proof


class DurableWeightSubmitter:
    def __init__(self, subtensor, hotkey, netuid, state_dir, genesis_hash, send):
        self.subtensor = subtensor
        self.hotkey = hotkey
        self.netuid = netuid
        self.state_dir = state_dir
        self.genesis_hash = genesis_hash
        self.send = send

    def _directory(self, mechid):
        return str(Path(self.state_dir) / "weight-loop" / str(self.netuid)
                   / self.hotkey / str(mechid))

    def _result(self, success, message, status=None):
        from weight_loop import SubmitResult
        return SubmitResult(success, message, status)

    def _legacy_fence(self):
        _guard_genesis(self.subtensor, self.genesis_hash)
        journal = _journal_for(self.state_dir)
        try:
            if journal.restore_fenced:
                return "legacy journal restore fence active"
            if journal.unresolved(self.hotkey, OperationKind.WEIGHTS_SUBMIT):
                return "legacy weights operation remains unresolved"
        finally:
            journal.close()
        return None

    def _query(self, name, params, head):
        return read_storage(self.subtensor.substrate, name, params, head.hash)

    def _row(self, mechid, head):
        count = mechanism_count(self._query("MechanismCountCurrent", [self.netuid], head))
        index = mechanism_index(self.netuid, mechid, count)
        uid = self._query("Uids", [self.netuid, self.hotkey], head)
        if uid is None:
            return (), None
        uint(uid, "signer UID", 65535)
        if self._query("Keys", [self.netuid, uid], head) != self.hotkey:
            raise ChainError("signer UID does not match its hotkey at the finalized block")
        updates = self._query("LastUpdate", [index], head)
        last = last_update_for_uid(updates, uid, head.block)
        row = self._query("Weights", [index, uid], head)
        if row is None:
            row = []
        return tuple((int(u), int(w)) for u, w in row), last

    def _pending(self, mechid, bound, head):
        # Enumerate actual native epoch keys: bundle_seq=head//tempo is unrelated
        # to these keys and may decrease on a tempo change. A bounded incomplete
        # scan fails closed; it is never evidence that no pending commit exists.
        index = self.netuid + mechid * 4096
        entries = self.subtensor.substrate.query_map(
            module="SubtensorModule", storage_function="TimelockedWeightCommits",
            params=[index], block_hash=head.hash,
        )
        for count, (epoch, _) in enumerate(entries):
            if count >= 64:
                raise ChainError("pending commit scan exceeds safety bound")
            epoch = int(getattr(epoch, "value", epoch))
            for commit in read_timelocked_commits(
                self.subtensor, index, epoch, block_hash=head.hash,
            ):
                if commit.who == self.hotkey and commit.commit_block >= bound:
                    return True
        return False

    def _reconcile_record(self, mechid, journal, rec, head):
        store = ArtifactStore(self._directory(mechid))
        raw = store.load(self.netuid, rec.key.subnet_epoch_index, rec.key.epoch_start_block)
        if raw is None or artifact_digest(raw) != rec.digest:
            raise ChainError("durable dispatch artifact missing or changed")
        data = json.loads(raw)
        expected = tuple(tuple(pair) for pair in data["row"])
        bound = rec.intent_block
        # Commit inclusion advances LastUpdate on spec475, even when the prior
        # row already equals this attempt. A known pending commit always fences.
        if self._pending(mechid, 0, head):
            journal.observe_pending(rec.key)
            return self._result(True, "commit pending reveal", "pending_reveal")
        row, updated = self._row(mechid, head)
        proof_reason = "exact post-intent row/activity not observed"
        if (row == expected and updated is not None and updated >= bound
                and updated > data["last_update_before"]):
            uid = self._query("Uids", [self.netuid, self.hotkey], head)
            proof, proof_reason = find_application_proof(
                self.subtensor, netuid=self.netuid, mechid=mechid,
                hotkey=self.hotkey, uid=int(uid), expected_row=expected,
                intent_block=bound, last_update=updated, head=head,
            )
            if proof is not None:
                journal.observe(rec.key, ObservationRef(
                    f"finalized WeightsSet {proof.block} {proof.block_hash}; exact row; LastUpdate {updated}"
                ))
                return self._result(True, f"finalized WeightsSet {proof.block}; LastUpdate {updated}", "confirmed")
        deadline = rec.expiry_deadline
        if deadline is None:
            deadline = journal.stamp_expiry_deadline(
                rec.key, expiry_deadline(head.block, bound),
            )
        if head.block >= deadline:
            journal.mark_expired(rec.key, ObservationRef("mortal attempt expired; no pending commit"),
                                 finalized_block=head.block)
            return self._result(False, "prior attempt expired safely", "waiting")
        journal.mark_unknown(rec.key)
        return self._result(False, f"{proof_reason}; fenced until finalized block {deadline}", "waiting")

    def reconcile(self, mechid):
        reason = self._legacy_fence()
        if reason:
            return self._result(False, reason, "waiting")
        journal = _journal_for(self._directory(mechid))
        try:
            if journal.restore_fenced:
                return self._result(False, "weight-loop journal restore fence active", "waiting")
            records = journal.unresolved(self.hotkey, OperationKind.WEIGHTS_SUBMIT)
            if not records:
                return None
            return self._reconcile_record(mechid, journal, records[0], finalized_head(self.subtensor))
        finally:
            journal.close()

    def __call__(self, *, mechid, uids, weights, version_key, commit_reveal, hotkeys=None):
        pending = self.reconcile(mechid)
        if pending is not None:
            return pending
        if not probe_available(self.subtensor):
            return self._result(False, "submit probe unavailable", "waiting")
        head = finalized_head(self.subtensor)
        _, before = self._row(mechid, head)
        if before is None:
            return self._result(False, "NotRegistered", "alert")
        if self._pending(mechid, 0, head):
            return self._result(False, "pre-existing commit pending reveal", "pending_reveal")
        encoded_uids, encoded_weights = convert_weights_and_uids(uids, weights)
        identity = {} if hotkeys is None else {"expected_hotkeys": list(hotkeys), "expected_uids": list(uids)}
        artifact = json.dumps({
            **identity,
            "row": list(zip(encoded_uids, encoded_weights)),
            "commit_reveal": bool(commit_reveal), "last_update_before": before,
        }, sort_keys=True).encode()
        directory = self._directory(mechid)
        store = ArtifactStore(directory)
        # Dispatch block is a monotonic attempt identity. Multiple broadcasts in
        # one block are disallowed; NOT_DISPATCHED remains safely re-armable.
        key = OperationKey(self.genesis_hash, self.netuid, self.hotkey,
                           head.block, head.block, OperationKind.WEIGHTS_SUBMIT)
        binding = ArtifactBinding(artifact_digest(artifact), "weight-loop-row")
        journal = _journal_for(directory)
        try:
            rec = journal.record(key)
            if rec is not None and rec.state not in (JournalState.PREPARED, JournalState.NOT_DISPATCHED):
                return self._result(False, "dispatch block already attempted", "waiting")
            store.store(self.netuid, head.block, head.block, artifact)
            if rec is None:
                journal.prepare(key, binding)
            if journal.begin(key, intent_block=head.block, binding=binding) is None:
                return self._result(False, "dispatch intent already claimed", "waiting")
            result = dispatch(self.subtensor, lambda: self.send(
                mechid=mechid, uids=uids, weights=weights,
                version_key=version_key, commit_reveal=commit_reveal,
                **({} if hotkeys is None else {"hotkeys": hotkeys}),
            ))
            if result.outcome is DispatchOutcome.NOT_DISPATCHED:
                journal.mark_not_dispatched(key, ObservationRef(result.detail))
                return self._result(False, result.detail)
            if result.outcome is DispatchOutcome.FAILED:
                journal.mark_failed(key, ObservationRef(result.detail))
                return self._result(False, result.detail)
            # A failure to read the finalized head leaves durable INTENT behind;
            # restart recovery turns it UNKNOWN, never an unrecorded retry.
            after = finalized_head(self.subtensor)
            journal.stamp_expiry_deadline(key, expiry_deadline(after.block, head.block))
            return self._reconcile_record(mechid, journal, journal.record(key), after)
        finally:
            journal.close()
