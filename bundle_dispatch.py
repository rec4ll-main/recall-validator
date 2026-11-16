"""SDK dispatch adapter: tell "never broadcast" apart from "possibly landed".

bittensor 10.5.0 ``set_weights`` / ``set_commitment`` return ``success=False`` for
very different situations: the SDK's own rate-limit loop guard never ran
(``core/subtensor.py`` set_weights), a pre-send RPC read raised, the extrinsic was
finalized with ``ExtrinsicFailed``, or the submit subscription failed after
broadcast. In the sync SDK the only broadcast point is
``subtensor.substrate.submit_extrinsic`` (called from ``sign_and_send_extrinsic``,
also on the MEV-shield path). ``dispatch`` wraps that one method for the duration of
the SDK call and classifies the attempt:

- ``NOT_DISPATCHED``: submit was never called, so nothing left this process.
- ``FAILED``: exactly one submit returned a finalized receipt whose
  ``is_success`` is exactly ``False`` (``ExtrinsicFailed``; its nonce is consumed,
  so it can never be included again).
- ``SUCCEEDED``: the SDK reported success after exactly one submit.
- ``UNCERTAIN``: anything else (submit raised, receipt unreadable, more than one
  submit). The transaction may still land until its mortality deadline.

MEV Shield follows the SDK default (``BT_MEV_PROTECTION``); the validator does not
pass ``mev_protection``. On that path the only submit is the encrypted wrapper, the
inner call runs with an era of at most 8 blocks, and an inner rejection comes back as
``success=False`` with the wrapper's successful receipt. That is classified UNCERTAIN
(never FAILED), so it is fenced and becomes EXPIRED after the mortality deadline.

Every dispatch is mortal: ``MORTALITY_PERIOD`` is passed explicitly, and
``expiry_deadline`` turns a finalized head read *after* the SDK call into a block at
or after which the transaction can no longer be included.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

# Mortal era length passed to every SDK extrinsic (the SDK default, made explicit).
MORTALITY_PERIOD = 128
# A mortal era is valid in [birth, birth + period) with birth <= the finalized block the
# node reported while signing, which is <= any finalized head read after the SDK call
# returned. The extra period absorbs era power-of-two rounding and node lag.
EXPIRY_MARGIN_BLOCKS = 2 * MORTALITY_PERIOD

OUTCOME_REF_MAX = 400


class DispatchOutcome(Enum):
    NOT_DISPATCHED = "not_dispatched"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    outcome: DispatchOutcome
    detail: str
    response: object = None
    extrinsic_hash: str | None = None


class DispatchProbeError(Exception):
    """The submit probe cannot be installed; the caller must not dispatch."""


def _short(text: object) -> str:
    """Single-line ASCII text bounded to OUTCOME_REF_MAX bytes (fits ObservationRef)."""
    value = " ".join(str(text).split()).encode("ascii", "replace").decode("ascii")
    return value[:OUTCOME_REF_MAX] or "-"


def _hash_of(extrinsic) -> str | None:
    try:
        raw = extrinsic.extrinsic_hash
    except Exception:
        return None
    if isinstance(raw, (bytes, bytearray)):
        return "0x" + bytes(raw).hex()
    if isinstance(raw, str):
        return raw
    return None


def probe_available(subtensor) -> bool:
    """True when ``substrate.submit_extrinsic`` exists and can be wrapped."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None or not callable(getattr(substrate, "submit_extrinsic", None)):
        return False
    return hasattr(substrate, "__dict__")


def dispatch(subtensor, send: Callable[[], object]) -> DispatchResult:
    """Run ``send`` (one SDK extrinsic call) and classify whether it was broadcast."""
    if not probe_available(subtensor):
        raise DispatchProbeError("substrate.submit_extrinsic cannot be observed")
    substrate = subtensor.substrate
    had_own = "submit_extrinsic" in vars(substrate)
    original = substrate.submit_extrinsic
    calls: list[dict] = []

    def observed_submit(extrinsic, *args, **kwargs):
        entry = {"hash": _hash_of(extrinsic), "receipt": None, "error": None}
        calls.append(entry)
        try:
            receipt = original(extrinsic, *args, **kwargs)
        except BaseException as exc:
            entry["error"] = exc
            raise
        entry["receipt"] = receipt
        return receipt

    substrate.submit_extrinsic = observed_submit
    response = None
    raised: Exception | None = None
    try:
        response = send()
    except Exception as exc:  # SDK pre-send reads run outside its own try blocks
        raised = exc
    finally:
        if had_own:
            substrate.submit_extrinsic = original
        else:
            del substrate.submit_extrinsic
    return _classify(calls, response, raised)


def _response_text(response, raised) -> str:
    if raised is not None:
        return f"{type(raised).__name__}: {raised}"
    message = getattr(response, "message", None)
    return str(message) if message else "unsuccessful response"


def _classify(calls, response, raised) -> DispatchResult:
    if not calls:
        return DispatchResult(
            DispatchOutcome.NOT_DISPATCHED,
            _short(f"not_dispatched: {_response_text(response, raised)}"),
            response,
        )
    if len(calls) > 1:
        return DispatchResult(
            DispatchOutcome.UNCERTAIN,
            _short(f"uncertain: {len(calls)} submissions in one attempt"),
            response,
            calls[-1]["hash"],
        )
    call = calls[0]
    if call["error"] is not None:
        err = call["error"]
        return DispatchResult(
            DispatchOutcome.UNCERTAIN,
            _short(f"uncertain: submit raised after broadcast attempt: {type(err).__name__}: {err}"),
            response,
            call["hash"],
        )
    if raised is None and getattr(response, "success", False) is True:
        return DispatchResult(DispatchOutcome.SUCCEEDED, "succeeded", response, call["hash"])
    receipt = call["receipt"]
    try:
        is_success = receipt.is_success
        finalized = getattr(receipt, "finalized", False)
        error_message = receipt.error_message if is_success is False else None
    except Exception as exc:
        return DispatchResult(
            DispatchOutcome.UNCERTAIN,
            _short(f"uncertain: receipt unreadable: {type(exc).__name__}: {exc}"),
            response,
            call["hash"],
        )
    if is_success is False and finalized is True:
        return DispatchResult(
            DispatchOutcome.FAILED,
            _short(f"failed: extrinsic finalized with ExtrinsicFailed: {error_message}"),
            response,
            call["hash"],
        )
    return DispatchResult(
        DispatchOutcome.UNCERTAIN,
        _short(f"uncertain: {_response_text(response, raised)}"),
        response,
        call["hash"],
    )


def expiry_deadline(finalized_block_after_call: int, intent_block: int | None) -> int:
    """Finalized block at or after which the dispatched transaction cannot be included."""
    return max(int(finalized_block_after_call), int(intent_block or 0)) + EXPIRY_MARGIN_BLOCKS


def weights_send_precondition(subtensor, wallet_hotkey: str, netuid: int) -> str | None:
    """Re-check the SDK 10.5.0 set_weights no-dispatch guards before writing INTENT.

    Returns None when the SDK would attempt a submission, else a blocked reason. The
    reads match the SDK's own (latest head, no block argument), so a pass here means
    the SDK loop guard will run unless the chain moves in between, which ``dispatch``
    then reports as NOT_DISPATCHED.
    """
    try:
        uid = subtensor.get_uid_for_hotkey_on_subnet(wallet_hotkey, netuid)
        if uid is None:
            return f"hotkey not registered on netuid {netuid}"
        blocks_since = subtensor.blocks_since_last_update(netuid, uid)
        rate_limit = subtensor.weights_rate_limit(netuid)
    except Exception as exc:
        return f"send precondition read failed: {type(exc).__name__}: {exc}"
    if blocks_since is None or rate_limit is None:
        return "send precondition unavailable (LastUpdate or WeightsSetRateLimit)"
    if not int(blocks_since) > int(rate_limit):
        return (
            f"weights rate limit: {blocks_since} blocks since last update <= "
            f"rate limit {rate_limit}"
        )
    return None
