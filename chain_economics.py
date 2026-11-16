"""Master-only relay of on-chain economics to the API for the public miners page.

At most every CHAIN_ECONOMICS_INTERVAL_SECONDS the master reads a lite metagraph
(incentive and validator trust per UID), its own weight row and the subnet's MinerBurned share, and POSTs
one snapshot to /api/validator/chain-economics with the internal API key. Best-effort:
a failure is logged without the URL, key or raw exception text and never touches the
weight or bundle paths.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request

from api_transport import ApiDestinationError, internal_key_headers, send_request, validated_endpoint
from validator_http import VALIDATOR_USER_AGENT

logger = logging.getLogger(__name__)

CHAIN_ECONOMICS_PATH = "/api/validator/chain-economics"
CHAIN_ECONOMICS_INTERVAL_SECONDS = 300
_last_attempt: float | None = None


def _share(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, f)) if f == f else None


def weight_row(subtensor, netuid: int, uid: int) -> dict[int, float]:
    """The validator's on-chain weight row, normalized to shares (empty when unset)."""
    raw = subtensor.substrate.query("SubtensorModule", "Weights", [int(netuid), int(uid)])
    pairs = getattr(raw, "value", raw) or []
    row = {int(dest): float(w) for dest, w in pairs}
    total = sum(row.values())
    return {d: w / total for d, w in row.items()} if total > 0 else {}


def build_snapshot(subtensor, netuid: int, validator_hotkey: str, miner_burned_share) -> dict | None:
    """One chain read: incentive and vtrust per UID, this validator's weights and MinerBurned."""
    mg = subtensor.metagraph(int(netuid), lite=True)
    hotkeys = [str(h) for h in mg.hotkeys]
    if validator_hotkey not in hotkeys:
        return None
    index = hotkeys.index(validator_hotkey)
    uids = [int(u) for u in mg.uids]
    weights = weight_row(subtensor, netuid, uids[index])
    # Validator trust (vtrust) lets the API tell validators from miners on the public
    # miner lists. Left out when the metagraph does not carry it.
    vtrust = getattr(mg, "Tv", None)
    if vtrust is not None and len(vtrust) != len(uids):
        vtrust = None
    neurons = []
    for i, (uid, hk) in enumerate(zip(uids, hotkeys)):
        n = {"uid": uid, "hotkey": hk, "incentive": _share(mg.I[i]) or 0.0,
             "validator_weight": weights.get(uid, 0.0) if weights else None}
        if vtrust is not None:
            n["validator_trust"] = _share(vtrust[i]) or 0.0
        neurons.append(n)
    return {"netuid": int(netuid), "block": int(mg.block), "miner_burned_share": _share(miner_burned_share),
            "validator_hotkey": validator_hotkey, "weights_last_update": int(mg.last_update[index]),
            "neurons": neurons}


def publish(api_url: str, internal_api_key: str, snapshot: dict, timeout: int = 10, urlopen=None) -> bool:
    if not api_url or not internal_api_key:
        return False
    try:
        endpoint = validated_endpoint(api_url, CHAIN_ECONOMICS_PATH)
    except ApiDestinationError as exc:
        logger.warning("Refusing to publish chain economics: %s", exc)
        return False
    payload = json.dumps(snapshot).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, method="POST",
                                     headers={**internal_key_headers(internal_api_key), "User-Agent": VALIDATOR_USER_AGENT})
    try:
        with send_request(request, timeout=timeout, urlopen=urlopen) as response:
            if response.status >= 300:
                logger.warning("chain economics publish rejected: HTTP %s", response.status)
                return False
            return True
    except Exception as exc:  # noqa: BLE001 - best-effort status relay
        logger.warning("chain economics publish failed: %s", type(exc).__name__)
        return False


def maybe_publish(subtensor, wallet, env, now: float | None = None, urlopen=None) -> bool:
    """Master only, rate-limited; returns True when a snapshot was accepted."""
    global _last_attempt
    if getattr(env, "bundle_role", "") != "master":
        return False
    now = time.time() if now is None else now
    if _last_attempt is not None and now - _last_attempt < CHAIN_ECONOMICS_INTERVAL_SECONDS:
        return False
    _last_attempt = now
    from loop_status import read_miner_burned

    burned = read_miner_burned(subtensor, env.netuid).get("share")
    snapshot = build_snapshot(subtensor, env.netuid, wallet.hotkey.ss58_address, burned)
    if snapshot is None:
        logger.warning("chain economics not published: validator hotkey not registered")
        return False
    return publish(env.recall_api_url, env.internal_api_key, snapshot, urlopen=urlopen)
