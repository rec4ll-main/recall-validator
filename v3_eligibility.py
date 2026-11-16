"""Allocator eligibility scope: which persisted v3 state a master reads and writes.

INCENTIVE_V3_ELIGIBILITY_SCOPE is empty (state in BUNDLE_STATE_DIR) or "grounded-v1"
(state in BUNDLE_STATE_DIR/v3_eligibility/weights/grounded-v1). A non-empty scope is
bound to every signed score_version 11 artifact before it enters the store, and new
publication re-checks that binding.
"""
from __future__ import annotations

import json
from pathlib import Path

GROUNDED_SCOPE = "grounded-v1"
MAX_BINDING_BYTES = 65536


class EligibilityScopeError(ValueError):
    """Missing or mismatched eligibility scope binding (fail closed)."""


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise EligibilityScopeError("duplicate artifact scope binding key")
        obj[key] = value
    return obj


def eligibility_state_dir(state_dir, mode, selection):
    """Allocator state only; dispatch journal/fences remain in the common directory."""
    if mode != "weights":
        raise EligibilityScopeError("invalid v3 eligibility mode")
    root = Path(state_dir) / "v3_eligibility" / mode
    if selection == GROUNDED_SCOPE:
        return str(root / GROUNDED_SCOPE)
    if selection:
        raise EligibilityScopeError("unsupported v3 eligibility scope")
    if root.exists() and any(root.glob("benchmark-*")):
        # State left by a retired benchmark scope: never fall back to the common
        # directory silently.
        raise EligibilityScopeError("benchmark history exists; explicitly select grounded-v1 eligibility scope")
    return state_dir


def _binding_path(cfg, digest):
    return Path(cfg.state_dir) / "v3_artifact_policies" / (digest.removeprefix("0x") + ".json")


def _binding_bytes(digest, scope):
    from v3_state import canonical

    # "generation_benchmark" is always null; the key is kept so bindings already on
    # disk keep matching byte for byte.
    return canonical({"artifact_digest": digest, "generation_benchmark": None,
                      "eligibility_scope": scope})


def bind_eligibility_artifact(cfg, envelope):
    """Persist the eligibility scope binding before a scoped envelope enters the store."""
    if not cfg.v3_eligibility_scope:
        return
    from bundle_store import artifact_digest
    from v3_state import _atomic_create

    digest = artifact_digest(envelope)
    path = _binding_path(cfg, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _binding_bytes(digest, cfg.v3_eligibility_scope)
    try:
        _atomic_create(path, raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise EligibilityScopeError("conflicting artifact eligibility scope binding") from None


def validate_eligibility_artifact(cfg, envelope):
    """Admission for NEW publication, never unresolved journal reconciliation."""
    from bundle_store import artifact_digest
    from bundle_v2 import parse_bundle

    # Gate the actual signed v11 wire artifact.
    if parse_bundle(envelope).payload.score_version != "11":
        return
    digest = artifact_digest(envelope)
    path = _binding_path(cfg, digest)
    eligibility_state_dir(cfg.state_dir, "weights", cfg.v3_eligibility_scope)
    if path.exists():
        try:
            with path.open("rb") as f:
                raw = f.read(MAX_BINDING_BYTES + 1)
            if len(raw) > MAX_BINDING_BYTES:
                raise EligibilityScopeError("artifact eligibility scope binding is too large")
            obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EligibilityScopeError("artifact eligibility scope binding unavailable") from exc
        if (not isinstance(obj, dict) or set(obj) != {"artifact_digest", "generation_benchmark", "eligibility_scope"} or
                obj["artifact_digest"] != digest or obj["eligibility_scope"] != cfg.v3_eligibility_scope or
                obj["generation_benchmark"] is not None):
            raise EligibilityScopeError("artifact eligibility scope binding mismatch")
    elif cfg.v3_eligibility_scope:
        raise EligibilityScopeError("artifact eligibility scope binding missing")
