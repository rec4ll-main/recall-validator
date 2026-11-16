"""Build the signed immutable allocation envelope for the master publish path.

The envelope is the score_version 11 (incentive v3) allocation signed by the master
hotkey (``v3_runtime.build_envelope_v3``). It is immutable: it is persisted and reused
byte-for-byte by the publish orchestration.
"""

from __future__ import annotations

from bundle_runtime import BundleRuntimeError


def build_envelope(subtensor, metagraph, wallet, cfg, schedule, *, fetch=None) -> bytes:
    """Fetch v3 evidence, allocate, and sign the immutable envelope.

    ``fetch`` optionally replaces the v3 evidence client (tests); production passes None.
    Any failure is raised as ``BundleRuntimeError`` so the publish path fails closed.
    """
    from v3_runtime import build_envelope_v3

    try:
        return build_envelope_v3(subtensor, metagraph, wallet, cfg, schedule, fetch=fetch)
    except Exception as exc:
        raise BundleRuntimeError(f"v3 envelope: {type(exc).__name__}: {exc}") from exc
