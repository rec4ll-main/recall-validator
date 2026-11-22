"""Deterministic in-memory chain + SDK fixture for chain-v2 consume/publish tests.

``FakeChain`` models the finalized chain state the validator reads: block hashes,
epoch schedule, ``Commitments.CommitmentOf``, ``SubtensorModule.TimelockedWeightCommits``,
weights rows with LastUpdate, the native max-weight cap, per-account nonces, and a
transaction pool with mortality. ``FakeSubtensor`` exposes the SDK surface the
validator calls. Its ``set_weights`` / ``set_commitment`` follow the control flow of
bittensor 10.5.0 (``core/subtensor.py`` set_weights rate-limit loop, the extrinsic
``try`` blocks, and ``sign_and_send_extrinsic``): the only broadcast point is
``substrate.submit_extrinsic``. Tests script per-submission behaviour with
``chain.submit_behaviors``.

MEV Shield: the validator does not pass ``mev_protection``, so the SDK default
(``BT_MEV_PROTECTION``, read into ``bittensor.core.settings.DEFAULT_MEV_PROTECTION``)
applies. The fixture reads that setting at call time and, when it is on, follows
``core/extrinsics/mev_shield.py``: the real call is signed at nonce N+1 with an era of
at most 8 blocks and wrapped in a ``mev_shield`` extrinsic at nonce N, which is the
only thing passed to ``submit_extrinsic``. The block author executes the inner call
in the wrapper's block. An inner rejection returns ``success=False`` carrying the
*wrapper's* successful receipt, so the dispatch adapter cannot see it as a definitive
FAILED: the attempt is UNCERTAIN, stays fenced as UNKNOWN, and becomes EXPIRED after
its mortality deadline (never retried for its epoch). That is accepted behaviour.
Script inner outcomes with ``chain.mev_inner_behaviors`` ("ok" | "fail").
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from async_substrate_interface.errors import SubstrateRequestException
from bittensor.core.types import ExtrinsicResponse
from bittensor_wallet import Keypair

from bundle_ipfs import compute_raw_cid
from bundle_runtime import BundleConfig
from bundle_schedule import PUBLICATION_CUTOFF_BLOCKS
from bundle_v2 import sign_bundle
from score_version import SUPPORTED_PROFILE

GENESIS = "0x" + "ab" * 32
NETUID = 31
TEMPO = 360
U16_MAX = 65535
MAX_MEV_SHIELD_PERIOD = 8  # bittensor.core.settings.MAX_MEV_SHIELD_PERIOD
_UNSET = object()


def sdk_default_mev_protection() -> bool:
    from bittensor.core import settings

    return bool(settings.DEFAULT_MEV_PROTECTION)


def block_hash(n: int) -> str:
    if n == 0:
        return GENESIS
    return "0x" + format(0x5000 + n, "064x")


def _block_of(h: str) -> int:
    if h == GENESIS:
        return 0
    return int(h, 16) - 0x5000


def keypair(name: str) -> Keypair:
    return Keypair.create_from_uri(f"//RecallFixture{name}")


OWNER = keypair("Owner")
OWNER_COLD = keypair("OwnerCold")
MASTER = keypair("Master")
SLAVE = keypair("Slave")
MINERS = [keypair(f"Miner{i}") for i in range(4)]
MINER_COLDS = [keypair(f"MinerCold{i}") for i in range(4)]
VALIDATOR_COLDS = [keypair("MasterCold"), keypair("SlaveCold")]

# uid 0 = subnet owner (never weighted), 1 = master, 2 = slave, 3..6 = miners.
HOTKEYS = [OWNER.ss58_address, MASTER.ss58_address, SLAVE.ss58_address] + [
    k.ss58_address for k in MINERS
]
COLDKEYS = [OWNER_COLD.ss58_address] + [k.ss58_address for k in VALIDATOR_COLDS] + [
    k.ss58_address for k in MINER_COLDS
]
REG_BLOCKS = [1, 2, 3, 10, 11, 12, 13]
MINER_UIDS = [3, 4, 5, 6]


class FakeWallet:
    def __init__(self, kp: Keypair):
        self.hotkey = kp


@dataclass
class FakeExtrinsic:
    kind: str  # "weights" | "commit_weights" | "commitment"
    signer: str
    nonce: int
    birth: int
    period: int
    payload: object
    extrinsic_hash: bytes = b""

    def __post_init__(self):
        raw = json.dumps(
            [self.kind, self.signer, self.nonce, self.birth, repr(self.payload)]
        ).encode()
        self.extrinsic_hash = hashlib.blake2b(raw, digest_size=32).digest()

    @property
    def value(self):
        return {"nonce": self.nonce, "era": {"period": self.period, "current": self.birth}}

    @property
    def death(self) -> int:
        return self.birth + self.period


@dataclass
class FakeReceipt:
    is_success: bool
    error_message: dict | None
    block_hash: str
    extrinsic_hash: str
    finalized: bool = True
    total_fee_amount: int = 0


@dataclass
class FakeChain:
    netuid: int = NETUID
    head: int = 0
    tempo: int = TEMPO
    native_cap: float = 1.0
    rate_limit: int = 100
    commit_reveal: bool = False
    state_pruned_below: int = 0
    # name -> exception (every call) or list of exceptions/None (one per call) raised by
    # that read: pre-send RPC failure injection.
    failing_reads: dict = field(default_factory=dict)
    # FIFO of behaviours for the next submit_extrinsic calls:
    #   "ok"      included + finalized, call executes (chain-side checks apply)
    #   "fail"    included + finalized, ExtrinsicFailed (nonce consumed)
    #   "timeout" broadcast into the pool, then the RPC raises (may land later)
    #   "drop"    the RPC raises and the transaction is gone
    #   "late"    included + finalized one block later than "ok" (lands after a cutoff)
    #   "crash"   included + finalized, then the process dies (SystemExit) before the
    #             SDK returns
    submit_behaviors: list = field(default_factory=list)
    submissions: list = field(default_factory=list)
    mev_inner_behaviors: list = field(default_factory=list)
    mev_inner_errors: dict = field(default_factory=dict)
    pool: list = field(default_factory=list)
    nonces: dict = field(default_factory=dict)  # ss58 -> [(block, nonce)]
    commitments: dict = field(default_factory=dict)  # ss58 -> [(block, cid)]
    weights_rows: dict = field(default_factory=dict)  # uid -> [(block, row)]
    commit_updates: dict = field(default_factory=dict)  # uid -> commit activity blocks
    timelocked: dict = field(default_factory=dict)  # epoch -> [(who, block, ct, round)]
    applied_weight_sets: list = field(default_factory=list)  # (block, uid, row)
    # Commitments pallet space model (spec 477): UsedSpaceOf resets only when the chain
    # SubnetEpochIndex changes. ``frozen_epoch_block`` freezes that index (commitment stall).
    max_space: int = 3100
    frozen_epoch_block: int | None = None
    used_space: dict = field(default_factory=dict)  # ss58 -> (epoch_index, used)

    def chain_epoch_block(self, block: int) -> int:
        if self.frozen_epoch_block is not None:
            return self.frozen_epoch_block
        return self.epoch_start(block)

    def chain_epoch_index(self, block: int) -> int:
        return self.chain_epoch_block(block) // self.tempo

    # -- schedule ------------------------------------------------------- #
    def epoch_start(self, block: int) -> int:
        return (block // self.tempo) * self.tempo

    def schedule_at(self, block: int):
        e = self.epoch_start(block)
        return SimpleNamespace(
            last_epoch_block=e,
            subnet_epoch_index=e // self.tempo,
            tempo=self.tempo,
            pending_epoch_at=e + self.tempo,
        )

    def advance(self, n: int = 1) -> None:
        for _ in range(n):
            self.head += 1
            self._drop_expired()

    def advance_to(self, block: int) -> None:
        if block > self.head:
            self.advance(block - self.head)

    def _drop_expired(self) -> None:
        self.pool = [x for x in self.pool if self.head < x.death]

    # -- history helpers ------------------------------------------------ #
    @staticmethod
    def _at(history, block):
        value = None
        for b, v in history:
            if b <= block:
                value = v
        return value

    def nonce_at(self, ss58: str, block: int) -> int:
        return self._at(self.nonces.get(ss58, []), block) or 0

    def _bump_nonce(self, ss58: str, block: int) -> None:
        self.nonces.setdefault(ss58, []).append((block, self.nonce_at(ss58, block) + 1))

    def last_update(self, uid: int, block: int) -> int:
        commits = [b for b in self.commit_updates.get(uid, []) if b <= block]
        if self.commit_reveal and commits:
            return commits[-1]  # CR reveal itself does not advance LastUpdate.
        hist = [b for b, _ in self.weights_rows.get(uid, []) if b <= block]
        return hist[-1] if hist else 0

    def row_at(self, uid: int, block: int):
        return self._at(self.weights_rows.get(uid, []), block)

    def pointer_at(self, ss58: str, block: int):
        hist = [(b, c) for b, c in self.commitments.get(ss58, []) if b <= block]
        return hist[-1] if hist else None

    # -- transaction inclusion ------------------------------------------ #
    def _execute(self, ext: FakeExtrinsic, block: int) -> dict | None:
        """Apply the call at ``block``; return a dispatch error or None."""
        if ext.kind == "mev_shield":
            # The author decrypts and executes the inner signed call in this block.
            inner = ext.payload
            assert self.head < inner.death, "inner MEV call included after its era"
            assert inner.nonce == self.nonce_at(inner.signer, block), "inner nonce mismatch"
            self._bump_nonce(inner.signer, block)
            behavior = self.mev_inner_behaviors.pop(0) if self.mev_inner_behaviors else "ok"
            inner_err = (
                {"type": "Module", "name": "Forced", "docs": ""}
                if behavior == "fail" else self._execute(inner, block)
            )
            self.mev_inner_errors[ext.extrinsic_hash] = inner_err
            return None  # the wrapper itself succeeds
        if ext.kind == "commitment":
            idx = self.chain_epoch_index(block)
            last, used = self.used_space.get(ext.signer, (idx, 0))
            used = used if last == idx else 0
            # Runtime floor applies once to the total Raw payload, not each byte/field.
            # Keep this model independent of the validator's preflight calculation.
            cost = max(len(ext.payload.encode("utf-8")), 100)
            if used + cost > self.max_space:
                return {"type": "Module", "name": "SpaceLimitExceeded",
                        "docs": ["Space Limit Exceeded for the current interval"]}
            self.used_space[ext.signer] = (idx, used + cost)
            self.commitments.setdefault(ext.signer, []).append((block, ext.payload))
            return None
        uid = HOTKEYS.index(ext.signer)
        if block - self.last_update(uid, block) <= self.rate_limit and self.last_update(uid, block):
            return {"type": "Module", "name": "SettingWeightsTooFast", "docs": ""}
        uids, values = ext.payload
        total = sum(values)
        # subtensor max_weight_limited / check_vec_max_limited in I32F32 (truncating).
        limit = round(self.native_cap * 65535)
        if limit < 65535 and max((v << 32) // total for v in values) > (limit << 32) // 65535:
            return {"type": "Module", "name": "MaxWeightExceeded", "docs": ""}
        row = tuple(zip(uids, values))
        if ext.kind == "commit_weights":
            epoch = self.schedule_at(block).subnet_epoch_index
            self.timelocked.setdefault(epoch, []).append((ext.signer, block, "00", 1))
            self._pending_reveals = getattr(self, "_pending_reveals", [])
            self._pending_reveals.append((epoch, ext.signer, uid, row))
            self.commit_updates.setdefault(uid, []).append(block)
            return None
        self.weights_rows.setdefault(uid, []).append((block, row))
        self.applied_weight_sets.append((block, uid, row))
        return None

    def include(self, ext: FakeExtrinsic, *, force_fail: bool = False) -> FakeReceipt:
        """Include ``ext`` in the next block and finalize it."""
        assert self.head < ext.death, "mortal transaction included after its era"
        assert ext.nonce == self.nonce_at(ext.signer, self.head), "nonce mismatch"
        self.advance(1)
        block = self.head
        self._bump_nonce(ext.signer, block)
        err = {"type": "Module", "name": "Forced", "docs": ""} if force_fail else self._execute(ext, block)
        return FakeReceipt(
            is_success=err is None,
            error_message=err,
            block_hash=block_hash(block),
            extrinsic_hash="0x" + ext.extrinsic_hash.hex(),
        )

    def include_pool(self) -> None:
        """Land every still-valid pooled (timed-out) transaction."""
        pending, self.pool = self.pool, []
        for ext in pending:
            self.include(ext)

    def reveal_all(self) -> None:
        """Reveal every pending commit-reveal commit (applies its row)."""
        self.advance(1)
        for epoch, who, uid, row in getattr(self, "_pending_reveals", []):
            self.timelocked[epoch] = [c for c in self.timelocked.get(epoch, []) if c[0] != who]
            self.weights_rows.setdefault(uid, []).append((self.head, row))
            self.applied_weight_sets.append((self.head, uid, row))
        self._pending_reveals = []

    def drop_commits(self) -> None:
        """A commit expires unrevealed (e.g. reveal failed); the row never changes."""
        for epoch in list(self.timelocked):
            self.timelocked[epoch] = []
        self._pending_reveals = []


class FakeSubstrate:
    def __init__(self, chain: FakeChain):
        self._chain = chain

    def get_chain_finalised_head(self):
        return block_hash(self._chain.head)

    def get_block_number(self, h):
        return _block_of(h)

    def get_chain_head(self):
        return block_hash(self._chain.head)

    def get_account_next_index(self, ss58):
        c = self._chain
        return c.nonce_at(ss58, c.head) + sum(1 for x in c.pool if x.signer == ss58)

    def query(self, module, storage_function, params, block_hash=None):
        c = self._chain
        block = _block_of(block_hash) if block_hash else c.head
        if block < c.state_pruned_below:
            raise Exception(f"State already discarded for block {block}")
        if (module, storage_function) == ("System", "Events"):
            return SimpleNamespace(value=[
                {"module_id": "SubtensorModule", "event_id": "WeightsSet", "attributes": [c.netuid, uid]}
                for uid, history in c.weights_rows.items() for at, _ in history if at == block
            ])
        if (module, storage_function) == ("SubtensorModule", "Weights"):
            return SimpleNamespace(value=c.row_at(params[1], block) or [])
        if (module, storage_function) == ("Commitments", "CommitmentOf"):
            entry = c.pointer_at(params[1], block)
            if entry is None:
                return SimpleNamespace(value=None)
            b, cid = entry
            return SimpleNamespace(
                value={
                    "deposit": 0,
                    "block": b,
                    "info": {"fields": [{f"Raw{len(cid)}": "0x" + cid.encode().hex()}]},
                }
            )
        if (module, storage_function) == ("Commitments", "MaxSpace"):
            return SimpleNamespace(value=c.max_space)
        if (module, storage_function) == ("Commitments", "UsedSpaceOf"):
            last, used = c.used_space.get(params[1], (0, 0))
            return SimpleNamespace(value={"last_epoch": last, "used_space": used})
        if (module, storage_function) == ("SubtensorModule", "SubnetEpochIndex"):
            return SimpleNamespace(value=c.chain_epoch_index(block))
        if (module, storage_function) == ("SubtensorModule", "LastEpochBlock"):
            return SimpleNamespace(value=c.chain_epoch_block(block))
        if (module, storage_function) == ("SubtensorModule", "Tempo"):
            return SimpleNamespace(value=c.tempo)
        if (module, storage_function) == ("SubtensorModule", "TimelockedWeightCommits"):
            entries = [e for e in c.timelocked.get(params[1], []) if e[1] <= block]
            return SimpleNamespace(value=[list(e) for e in entries])
        if (module, storage_function) == ("SubtensorModule", "SubnetOwnerHotkey"):
            return SimpleNamespace(value=OWNER.ss58_address)
        if (module, storage_function) == ("SubtensorModule", "Uids"):
            return SimpleNamespace(value=HOTKEYS.index(params[1]) if params[1] in HOTKEYS else None)
        if (module, storage_function) == ("SubtensorModule", "Keys"):
            return SimpleNamespace(value=HOTKEYS[params[1]] if 0 <= params[1] < len(HOTKEYS) else None)
        if (module, storage_function) == ("System", "Account"):
            return SimpleNamespace(value={"nonce": c.nonce_at(params[0], block)})
        raise AssertionError(f"unexpected query {module}.{storage_function}")

    def query_map(self, module, storage_function, params, block_hash=None):
        assert (module, storage_function) == ("SubtensorModule", "TimelockedWeightCommits")
        block = _block_of(block_hash) if block_hash else self._chain.head
        if block < self._chain.state_pruned_below:
            raise Exception("State already discarded")
        return [(epoch, entries) for epoch, entries in self._chain.timelocked.items()
                if any(entry[1] <= block for entry in entries)]

    def submit_extrinsic(self, extrinsic, wait_for_inclusion=False, wait_for_finalization=False):
        c = self._chain
        c.submissions.append(extrinsic)
        behavior = c.submit_behaviors.pop(0) if c.submit_behaviors else "ok"
        if behavior == "ok":
            return c.include(extrinsic)
        if behavior == "fail":
            return c.include(extrinsic, force_fail=True)
        if behavior == "late":
            c.advance(1)
            return c.include(extrinsic)
        if behavior == "crash":
            c.include(extrinsic)
            raise SystemExit("process killed after broadcast")
        if behavior == "timeout":
            c.pool.append(extrinsic)
            raise SubstrateRequestException("Subscription finalityTimeout")
        if behavior == "drop":
            raise SubstrateRequestException("Subscription dropped")
        raise AssertionError(f"unknown behavior {behavior}")


class FakeSubtensor:
    """SDK-shaped facade over FakeChain (sync bittensor 10.5.0 call surface)."""

    def __init__(self, chain: FakeChain):
        self.chain = chain
        self.substrate = FakeSubstrate(chain)
        self.set_weights_calls = 0
        self.set_commitment_calls = 0
        # True when a caller passed mev_protection explicitly (the validator must not).
        self.mev_protection_passed: list[bool] = []
        self.mev_used: list[bool] = []

    def _resolve_mev(self, mev_protection) -> bool:
        self.mev_protection_passed.append(mev_protection is not _UNSET)
        mev = sdk_default_mev_protection() if mev_protection is _UNSET else bool(mev_protection)
        self.mev_used.append(mev)
        return mev

    def _send(self, ext, *, mev: bool, raise_error: bool):
        """Plain sign_and_send, or the MEV Shield wrapper (mev_shield.py control flow)."""
        if not mev:
            return self._sign_and_send(ext, raise_error=raise_error)
        period = min(ext.period, MAX_MEV_SHIELD_PERIOD)
        inner = FakeExtrinsic(
            kind=ext.kind, signer=ext.signer, nonce=ext.nonce + 1,
            birth=ext.birth, period=period, payload=ext.payload,
        )
        wrapper = FakeExtrinsic(
            kind="mev_shield", signer=ext.signer, nonce=ext.nonce,
            birth=ext.birth, period=period, payload=inner,
        )
        response = self._sign_and_send(wrapper, raise_error=raise_error)
        if not response.success:
            return response
        inner_err = self.chain.mev_inner_errors.get(wrapper.extrinsic_hash)
        if inner_err is not None:
            # SDK keeps the wrapper's (successful) receipt and flips success.
            response.success = False
            response.message = str(inner_err)
            response.error = SubstrateRequestException(str(inner_err))
        return response

    def _read(self, name):
        exc = self.chain.failing_reads.get(name)
        if isinstance(exc, list):
            exc = exc.pop(0) if exc else None
        if exc is not None:
            raise exc

    # -- reads ----------------------------------------------------------- #
    def get_block_hash(self, block):
        if block > self.chain.head:
            return None
        return block_hash(block)

    def get_current_block(self):
        self._read("get_current_block")
        return self.chain.head

    def get_epoch_schedule_state(self, netuid, block=None):
        return self.chain.schedule_at(self.chain.head if block is None else block)

    def max_weight_limit(self, netuid, block=None):
        return self.chain.native_cap

    def get_subnet_info(self, netuid):
        return SimpleNamespace(owner_ss58=OWNER_COLD.ss58_address, owner_hotkey=OWNER.ss58_address)

    def get_uid_for_hotkey_on_subnet(self, hotkey, netuid, block=None):
        self._read("get_uid_for_hotkey_on_subnet")
        return HOTKEYS.index(hotkey) if hotkey in HOTKEYS else None

    def blocks_since_last_update(self, netuid, uid, block=None):
        self._read("blocks_since_last_update")
        b = self.chain.head if block is None else block
        return b - self.chain.last_update(uid, b)

    def weights_rate_limit(self, netuid, block=None):
        self._read("weights_rate_limit")
        return self.chain.rate_limit

    def commit_reveal_enabled(self, netuid, block=None):
        self._read("commit_reveal_enabled")
        return self.chain.commit_reveal

    def weights(self, netuid, block=None):
        b = self.chain.head if block is None else block
        out = []
        for uid in range(len(HOTKEYS)):
            row = self.chain.row_at(uid, b)
            if row:
                out.append((uid, [list(p) for p in row]))
        return out

    # -- extrinsics (bittensor 10.5.0 control flow) ----------------------- #
    def _sign_and_send(self, ext, *, raise_error):
        """Mirror of Subtensor.sign_and_send_extrinsic (submit + receipt handling)."""
        response = ExtrinsicResponse(extrinsic_function="sign_and_send_extrinsic")
        try:
            receipt = self.substrate.submit_extrinsic(
                ext, wait_for_inclusion=True, wait_for_finalization=True
            )
            response.extrinsic_receipt = receipt
            if receipt.is_success:
                response.message = "Success"
                return response
            if raise_error:
                raise RuntimeError(receipt.error_message)
            response.success = False
            response.message = str(receipt.error_message)
            response.error = receipt.error_message
            return response
        except SubstrateRequestException as error:
            if raise_error:
                raise
            response.success = False
            response.message = str(error)
            response.error = error
            return response

    def _weights_extrinsic(self, wallet, uids, weights, *, period, raise_error, mev=False):
        """Mirror of set_weights_extrinsic / commit_timelocked_weights_extrinsic."""
        try:
            self.get_current_block()  # pre-send RPC inside the extrinsic try
            from allocation_encoding import convert_weights_and_uids

            _, values = convert_weights_and_uids(list(uids), list(weights))
            kind = "commit_weights" if self.chain.commit_reveal else "weights"
            signer = wallet.hotkey.ss58_address
            ext = FakeExtrinsic(
                kind=kind,
                signer=signer,
                nonce=self.substrate.get_account_next_index(signer),
                birth=self.chain.head,
                period=period,
                payload=(list(uids), values),
            )
            return self._send(ext, mev=mev, raise_error=raise_error)
        except Exception as error:
            return ExtrinsicResponse.from_exception(raise_error=raise_error, error=error)

    def set_weights(
        self, wallet, netuid, uids, weights, mechid=0, block_time=12.0,
        commit_reveal_version=4, max_attempts=5, version_key=0, *,
        mev_protection=_UNSET, period=128, raise_error=False,
        wait_for_inclusion=True, wait_for_finalization=True,
        wait_for_revealed_execution=True,
    ):
        self.set_weights_calls += 1
        mev = self._resolve_mev(mev_protection)
        attempt = 0
        response = ExtrinsicResponse(False)

        uid = self.get_uid_for_hotkey_on_subnet(wallet.hotkey.ss58_address, netuid)
        if uid is None:
            return ExtrinsicResponse(False, "Hotkey not registered in subnet.")

        def _blocks_weight_limit() -> bool:
            return self.blocks_since_last_update(netuid, uid) > self.weights_rate_limit(netuid)

        self.commit_reveal_enabled(netuid=netuid)
        while attempt < max_attempts and response.success is False and _blocks_weight_limit():
            try:
                response = self._weights_extrinsic(
                    wallet, uids, weights, period=period, raise_error=raise_error, mev=mev
                )
            except Exception as error:
                return ExtrinsicResponse.from_exception(raise_error=raise_error, error=error)
            attempt += 1
        return response

    def set_commitment(
        self, wallet, netuid, data, *, mev_protection=_UNSET, period=128,
        raise_error=False, wait_for_inclusion=True, wait_for_finalization=True,
        wait_for_revealed_execution=True,
    ):
        self.set_commitment_calls += 1
        mev = self._resolve_mev(mev_protection)
        try:
            self._read("publish_metadata")
            signer = wallet.hotkey.ss58_address
            ext = FakeExtrinsic(
                kind="commitment",
                signer=signer,
                nonce=self.substrate.get_account_next_index(signer),
                birth=self.chain.head,
                period=period,
                payload=data,
            )
            response = self._send(ext, mev=mev, raise_error=raise_error)
            if response.success:
                return response
            raise RuntimeError(response.message)
        except Exception as error:
            return ExtrinsicResponse.from_exception(raise_error=raise_error, error=error)


class FakeMetagraph:
    def __init__(self):
        self.hotkeys = []
        self.coldkeys = []
        self.block_at_registration = []

    def sync(self, subtensor=None, block=None, lite=True):
        self.hotkeys = list(HOTKEYS)
        self.coldkeys = list(COLDKEYS)
        self.block_at_registration = list(REG_BLOCKS)


def config(state_dir, *, ipfs_api_url="", gateway_urls=(), role="slave") -> BundleConfig:
    return BundleConfig(
        netuid=NETUID,
        genesis_hash=GENESIS,
        master_hotkey=MASTER.ss58_address,
        state_dir=str(state_dir),
        role=role,
        ipfs_api_url=ipfs_api_url,
        gateway_urls=tuple(gateway_urls) or (("https://fixture-gateway.example",) if ipfs_api_url and role == "slave" else ()),
        ipfs_allow_plaintext=True,
    )


def build_envelope_for(chain: FakeChain, subtensor, epoch_start: int, *, weights=None,
                       with_owner_row: bool = False, signer: Keypair = MASTER, **overrides) -> bytes:
    """Sign a valid bundle for the epoch at ``epoch_start`` (uses the real snapshot).
    ``with_owner_row`` adds a row for the subnet owner, which every check must refuse."""
    from allocation_snapshot import build_snapshot, snapshot_digest

    snap = build_snapshot(subtensor, FakeMetagraph(), NETUID, None, GENESIS, block=epoch_start)
    from allocation_encoding import convert_weights_and_uids

    uids = ([0] if with_owner_row else []) + list(MINER_UIDS)
    if weights is None:
        shares = ([0.8] + [0.05] * len(MINER_UIDS)) if with_owner_row else [0.25] * len(MINER_UIDS)
        _, weights = convert_weights_and_uids(list(uids), shares)
    payload = {
        "schema": "recall-allocation-bundle-v2",
        "genesis_hash": GENESIS,
        "netuid": NETUID,
        "master_hotkey": MASTER.ss58_address,
        "epoch_start_block": epoch_start,
        "subnet_epoch_index": epoch_start // chain.tempo,
        "selection_block": epoch_start + PUBLICATION_CUTOFF_BLOCKS,
        "anchor_block": snap.anchor_block,
        "anchor_hash": snap.anchor_hash,
        "score_version": SUPPORTED_PROFILE[0],
        "score_kind": SUPPORTED_PROFILE[1],
        "evidence_window_seconds": SUPPORTED_PROFILE[2],
        "snapshot_digest": snapshot_digest(snap),
        "uids": uids,
        "hotkeys": [HOTKEYS[u] for u in uids],
        "weights_u16": list(weights),
    }
    payload.update(overrides)
    if signer is MASTER:
        return sign_bundle(payload, MASTER)
    # Forged signature: sign with another key but claim the master hotkey.
    from bundle_v2 import canonical_bytes, signing_message
    from bundle_v2_types import validate_payload

    canonical = validate_payload(payload).to_dict()
    sig = signer.sign(signing_message(canonical)).hex()
    return json.dumps(
        {"payload": canonical, "signature": sig},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()


def publish_pointer(chain: FakeChain, envelope: bytes, block: int) -> str:
    """Record the master's on-chain pointer to ``envelope`` at ``block``."""
    cid = compute_raw_cid(envelope)
    chain.commitments.setdefault(MASTER.ss58_address, []).append((block, cid))
    return cid
