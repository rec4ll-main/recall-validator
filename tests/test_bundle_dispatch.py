"""The SDK dispatch adapter tells "never broadcast" from "possibly landed"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from async_substrate_interface.errors import SubstrateRequestException
from bittensor.core.types import ExtrinsicResponse

from bundle_dispatch import (
    EXPIRY_MARGIN_BLOCKS,
    DispatchOutcome,
    DispatchProbeError,
    dispatch,
    expiry_deadline,
    probe_available,
    weights_send_precondition,
)


class Substrate:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def submit_extrinsic(self, extrinsic, **kw):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _receipt(is_success, finalized=True):
    return SimpleNamespace(
        is_success=is_success, finalized=finalized,
        error_message=None if is_success else {"name": "MaxWeightExceeded"},
    )


def _send_via(sub, *, response_success=None, raise_after=None):
    def send():
        receipt = None
        try:
            receipt = sub.substrate.submit_extrinsic(SimpleNamespace(extrinsic_hash=b"\x01" * 32))
        except SubstrateRequestException as exc:
            return ExtrinsicResponse(False, str(exc))
        if raise_after is not None:
            raise raise_after
        ok = receipt.is_success if response_success is None else response_success
        return ExtrinsicResponse(bool(ok), "x")
    return send


def test_no_submit_call_is_not_dispatched():
    sub = SimpleNamespace(substrate=Substrate())
    out = dispatch(sub, lambda: ExtrinsicResponse(False, "too soon"))
    assert out.outcome is DispatchOutcome.NOT_DISPATCHED
    assert out.detail == "not_dispatched: too soon"


def test_exception_before_submit_is_not_dispatched():
    sub = SimpleNamespace(substrate=Substrate())

    def send():
        raise ConnectionError("websocket closed")

    out = dispatch(sub, send)
    assert out.outcome is DispatchOutcome.NOT_DISPATCHED
    assert "ConnectionError: websocket closed" in out.detail


def test_finalized_extrinsic_failed_is_failed():
    sub = SimpleNamespace(substrate=Substrate(result=_receipt(False)))
    out = dispatch(sub, _send_via(sub))
    assert out.outcome is DispatchOutcome.FAILED
    assert "MaxWeightExceeded" in out.detail
    assert out.extrinsic_hash == "0x" + "01" * 32


def test_unfinalized_failure_is_uncertain():
    sub = SimpleNamespace(substrate=Substrate(result=_receipt(False, finalized=False)))
    assert dispatch(sub, _send_via(sub)).outcome is DispatchOutcome.UNCERTAIN


def test_unknown_receipt_result_is_uncertain():
    sub = SimpleNamespace(substrate=Substrate(result=_receipt(None)))
    assert dispatch(sub, _send_via(sub, response_success=False)).outcome is DispatchOutcome.UNCERTAIN


def test_submit_error_after_attempt_is_uncertain():
    sub = SimpleNamespace(substrate=Substrate(error=SubstrateRequestException("finalityTimeout")))
    out = dispatch(sub, _send_via(sub))
    assert out.outcome is DispatchOutcome.UNCERTAIN
    assert "finalityTimeout" in out.detail


def test_sdk_error_after_successful_submit_is_uncertain():
    sub = SimpleNamespace(substrate=Substrate(result=_receipt(True)))
    out = dispatch(sub, _send_via(sub, raise_after=RuntimeError("post-processing")))
    assert out.outcome is DispatchOutcome.UNCERTAIN


def test_success_is_succeeded():
    sub = SimpleNamespace(substrate=Substrate(result=_receipt(True)))
    assert dispatch(sub, _send_via(sub)).outcome is DispatchOutcome.SUCCEEDED


def test_two_submissions_are_uncertain():
    sub = SimpleNamespace(substrate=Substrate(result=_receipt(True)))

    def send():
        sub.substrate.submit_extrinsic(object())
        sub.substrate.submit_extrinsic(object())
        return ExtrinsicResponse(True, "x")

    assert dispatch(sub, send).outcome is DispatchOutcome.UNCERTAIN


def test_probe_is_removed_after_the_call_even_on_base_exception():
    substrate = Substrate()
    sub = SimpleNamespace(substrate=substrate)

    def send():
        raise SystemExit("killed")

    with pytest.raises(SystemExit):
        dispatch(sub, send)
    assert "submit_extrinsic" not in vars(substrate)
    assert substrate.submit_extrinsic.__func__ is Substrate.submit_extrinsic


def test_probe_unavailable_refuses_to_dispatch():
    sub = SimpleNamespace(substrate=SimpleNamespace())
    assert not probe_available(sub)
    with pytest.raises(DispatchProbeError):
        dispatch(sub, lambda: None)


def test_outcome_detail_is_bounded_ascii():
    sub = SimpleNamespace(substrate=Substrate())
    out = dispatch(sub, lambda: ExtrinsicResponse(False, "\u00e9" * 2000))
    assert len(out.detail.encode()) <= 400
    assert out.detail.isascii()


def test_expiry_deadline_uses_the_later_bound():
    assert expiry_deadline(1000, 900) == 1000 + EXPIRY_MARGIN_BLOCKS
    assert expiry_deadline(800, 900) == 900 + EXPIRY_MARGIN_BLOCKS


def test_weights_send_precondition_mirrors_the_sdk_guard():
    def sub(uid=5, since=101, limit=100, raise_on=None):
        def read(value, name):
            def f(*a, **k):
                if raise_on == name:
                    raise ConnectionError("down")
                return value
            return f
        return SimpleNamespace(
            get_uid_for_hotkey_on_subnet=read(uid, "uid"),
            blocks_since_last_update=read(since, "since"),
            weights_rate_limit=read(limit, "limit"),
        )

    assert weights_send_precondition(sub(), "hk", 31) is None
    assert "rate limit" in weights_send_precondition(sub(since=100), "hk", 31)
    assert "not registered" in weights_send_precondition(sub(uid=None), "hk", 31)
    assert "read failed" in weights_send_precondition(sub(raise_on="since"), "hk", 31)
    assert "unavailable" in weights_send_precondition(sub(limit=None), "hk", 31)
