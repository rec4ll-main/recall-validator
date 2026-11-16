"""Client for recall-web POST /api/validator/v3/metrics (score_version 11).

Numeric-only metrics: per miner and tier, per-check (n_eff, p_hat), the latency x
capacity factor, binding-check trailing-H counts for the slot rule, optional credited
service units; per pool credited demand D; the exact server `t_call` used for every
window. Transport rules (validator_http helpers): HTTPS unless loopback or
explicit override, no userinfo, no redirects, 1 MiB cap, API key required.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from allocation_v3 import MinerEvidence, MinerTierEvidence
from validator_http import (
    NO_REDIRECT_OPENER as _NO_REDIRECT_OPENER,
    VALIDATOR_USER_AGENT,
    ApiFetchError,
    is_loopback as _is_loopback,
)
from v3_acceptance import CheckStats
from v3_params import (
    slot_window_hours,
    CHECKS,
    CHECKS_BY_TIER,
    SERVICE_POOLS,
    TIERS,
    V3_METRICS_SCHEMA,
    V3_EVIDENCE_WINDOW_SECONDS,
    V3_HALF_LIFE_SECONDS,
    V3_SCORE_KIND,
    V3_SCORE_VERSION,
)

MAX_RESPONSE_BYTES = 1 << 20
DEFAULT_TIMEOUT_SECONDS = 10


class V3MetricsError(ApiFetchError):
    """Fetch/validation failure for the v3 evidence endpoint (fail closed)."""


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKeyError("duplicate JSON key")
        obj[key] = value
    return obj


@dataclass(frozen=True, slots=True)
class V3Metrics:
    netuid: int
    epoch_start_block: int
    t_call: str
    demand: dict
    miners: tuple  # (uid, hotkey, serious_failure, {tier: MinerTierEvidence})


def _num(v: object, lo: float = 0.0, hi: float = math.inf) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise V3MetricsError("expected a number")
    f = float(v)
    if not math.isfinite(f) or f < lo or f > hi:
        raise V3MetricsError("number out of range")
    return f


def _int(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise V3MetricsError("expected a non-negative integer")
    return v


def _t_call(v: object) -> str:
    if not isinstance(v, str) or not v.endswith("Z"):
        raise V3MetricsError("t_call must be RFC3339 UTC")
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise V3MetricsError("t_call must be RFC3339 UTC") from exc
    return v


def parse_v3_metrics(body: object, netuid: int, epoch_start_block: int,
                      cohort: dict[str, int]) -> V3Metrics:
    if not isinstance(body, dict):
        raise V3MetricsError("invalid response")
    if body.get("schema") != V3_METRICS_SCHEMA:
        raise V3MetricsError("wrong schema")
    if body.get("netuid") != netuid or isinstance(body.get("netuid"), bool):
        raise V3MetricsError("netuid mismatch")
    if body.get("epoch_start_block") != epoch_start_block or isinstance(body.get("epoch_start_block"), bool):
        raise V3MetricsError("epoch_start_block mismatch")
    if body.get("score_version") != V3_SCORE_VERSION:
        raise V3MetricsError("unsupported score version")
    if body.get("score_kind") != V3_SCORE_KIND:
        raise V3MetricsError("wrong score kind")
    if body.get("window_seconds") != V3_EVIDENCE_WINDOW_SECONDS:
        raise V3MetricsError("wrong window")
    if body.get("half_life_seconds") != V3_HALF_LIFE_SECONDS:
        raise V3MetricsError("wrong half-life")
    want_slot = slot_window_hours() * 3600
    if body.get("slot_window_seconds") != want_slot:
        raise V3MetricsError(f"slot_window_seconds must be {want_slot} (INCENTIVE_V3_SLOT_WINDOW_HOURS)")
    if body.get("storage_rewards_enabled") is not False:
        raise V3MetricsError("storage rewards must be disabled")
    t_call = _t_call(body.get("t_call"))
    # Synthetic benchmark metadata is never accepted (fail closed, as before).
    if "generation_benchmark" in body:
        raise V3MetricsError("generation benchmark metadata is not accepted")
    raw_demand = body.get("demand")
    if not isinstance(raw_demand, dict) or set(raw_demand) != set(SERVICE_POOLS):
        raise V3MetricsError("demand must list every service pool")
    demand = {k: _num(raw_demand[k]) for k in SERVICE_POOLS}
    raw_miners = body.get("miners")
    if not isinstance(raw_miners, list):
        raise V3MetricsError("missing miners")
    seen: set = set()
    out = []
    for item in raw_miners:
        if not isinstance(item, dict):
            raise V3MetricsError("invalid miner entry")
        uid, hotkey = item.get("uid"), item.get("hotkey")
        if isinstance(uid, bool) or not isinstance(uid, int) or not isinstance(hotkey, str):
            raise V3MetricsError("invalid miner identity")
        if hotkey not in cohort or cohort[hotkey] != uid:
            raise V3MetricsError("foreign or mismatched miner")
        if hotkey in seen:
            raise V3MetricsError("duplicate miner")
        seen.add(hotkey)
        # Fail closed: a verdict the API left out is never read as "no serious failure".
        if "serious_failure" not in item:
            raise V3MetricsError("missing serious_failure")
        serious = item["serious_failure"]
        if not isinstance(serious, bool):
            raise V3MetricsError("serious_failure must be bool")
        tiers_raw = item.get("tiers")
        if not isinstance(tiers_raw, dict) or not set(tiers_raw) <= set(TIERS):
            raise V3MetricsError("invalid tiers")
        tiers = {}
        for tier, t in tiers_raw.items():
            if not isinstance(t, dict) or not isinstance(t.get("checks"), list):
                raise V3MetricsError("invalid tier evidence")
            checks = []
            for c in t["checks"]:
                if not isinstance(c, dict) or c.get("name") not in CHECKS_BY_TIER[tier]:
                    raise V3MetricsError("unknown check")
                blocked = c.get("blocked", "")
                if not isinstance(blocked, str) or len(blocked) > 128:
                    raise V3MetricsError("invalid blocked reason")
                n_eff, p_hat = _num(c.get("n_eff")), _num(c.get("p_hat"), 0.0, 1.0)
                if blocked and (n_eff != 0.0 or p_hat != 0.0):
                    raise V3MetricsError("blocked check must carry zero evidence")
                checks.append(CheckStats(c["name"], n_eff, p_hat, blocked))
            if len({c.name for c in checks}) != len(checks):
                raise V3MetricsError("duplicate check")
            credited = t.get("credited")
            if credited is not None:
                if not isinstance(credited, dict) or not set(credited) <= {
                        p for p, (pt, _) in SERVICE_POOLS.items() if pt == tier}:
                    raise V3MetricsError("invalid credited map")
                credited = {k: _num(v) for k, v in credited.items()}
            # Fail closed: a missing latency/capacity factor is never read as full marks (1.0).
            for field in ("factor", "h_passes", "h_n"):
                if field not in t:
                    raise V3MetricsError(f"missing {field}")
            h_passes, h_n = _int(t["h_passes"]), _int(t["h_n"])
            if h_passes > h_n:
                raise V3MetricsError("h_passes > h_n")
            tiers[tier] = MinerTierEvidence(tuple(checks), _num(t["factor"], 0.0, 1.0),
                                            h_passes, h_n, credited)
        out.append((uid, hotkey, serious, tiers))
    return V3Metrics(netuid, epoch_start_block, t_call, demand, tuple(out))


def to_miner_evidence(ev: V3Metrics, snapshot_cohort) -> list[MinerEvidence]:
    """Join evidence with the chain snapshot (uid, hotkey, coldkey, registration)."""
    meta = {hk: (uid, ck, reg) for uid, hk, ck, reg in snapshot_cohort}
    out = []
    for uid, hotkey, serious, tiers in ev.miners:
        if hotkey not in meta or meta[hotkey][0] != uid:
            raise V3MetricsError("evidence miner not in snapshot")
        _, ck, reg = meta[hotkey]
        out.append(MinerEvidence(uid, hotkey, ck, reg, tiers, serious))
    return out


def fetch_v3_metrics(api_url: str, internal_api_key: str, cohort: Sequence[tuple[int, str]],
                      netuid: int, epoch_start_block: int, *, timeout: int = DEFAULT_TIMEOUT_SECONDS,
                      urlopen: Callable | None = None, allow_plaintext: bool = False) -> V3Metrics:
    if not internal_api_key:
        raise V3MetricsError("internal API key is required")
    base = api_url.rstrip("/")
    path = "/api/validator/v3/metrics"
    if base.endswith("/api"):
        path = path.removeprefix("/api")
    endpoint = base + path
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise V3MetricsError("unsupported URL scheme")
    if parsed.username or parsed.password:
        raise V3MetricsError("URL userinfo is not allowed")
    if parsed.scheme == "http" and not allow_plaintext and not _is_loopback(parsed.hostname or ""):
        raise V3MetricsError("plaintext HTTP requires an explicit test override")
    request_body = {
        "netuid": netuid, "epoch_start_block": epoch_start_block,
        "miners": [{"uid": u, "hotkey": h} for u, h in cohort],
    }
    body = json.dumps(request_body).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Internal-API-Key": internal_api_key,
        "User-Agent": VALIDATOR_USER_AGENT})
    opener = urlopen if urlopen is not None else _NO_REDIRECT_OPENER.open
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except ApiFetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise V3MetricsError(f"v3 evidence HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise V3MetricsError(f"v3 evidence fetch failed: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise V3MetricsError("v3 evidence response exceeded 1 MiB")
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError) as exc:
        raise V3MetricsError("v3 evidence invalid JSON") from exc
    return parse_v3_metrics(decoded, netuid, epoch_start_block, {h: u for u, h in cohort})
