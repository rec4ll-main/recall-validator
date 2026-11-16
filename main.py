"""Minimal Recall subnet validator.

BUNDLE_MODE=chain-v2 is the only supported mode.
Master publishes a weight bundle, then both roles consume it (chain pointer, then IPFS).
A chain-v2 slave never calls the web API.

There are intentionally no HTTP endpoints here.
"""

import logging
import asyncio
import hashlib
import time
import urllib.error
import urllib.request
import uuid

import bittensor as bt
from bittensor_wallet import Wallet

from api_transport import (
    METAGRAPH_SYNC_PATH,
    NO_REDIRECT_OPENER as _NO_REDIRECT_OPENER,
    ApiDestinationError,
    NoRedirectHandler as _NoRedirect,
    api_endpoint_url,
    describe_transport_error,
    internal_key_headers,
    metagraph_sync_body,
    send_request,
    validated_endpoint,
)
from deployment_config import _is_placeholder, validate_deployment_secrets  # noqa: F401
from environment import EnvironmentVariables
from validator_http import VALIDATOR_USER_AGENT
from bundle_consume import slave_consume
from bundle_publish import master_publish, master_reconcile_pointer
from chain_v2_registry import build_registry_sync
from bundle_runtime import BundleConfig
from bundle_schedule import ScheduleError, finalized_head, read_schedule


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def epistula_headers(wallet, signed_for: str, body: bytes, request_uuid: str = "") -> dict[str, str]:
    """Create Epistula headers compatible with gobt's verifier."""
    request_uuid = request_uuid or str(uuid.uuid4())
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{body_hash}.{request_uuid}.{timestamp}.{signed_for}".encode("utf-8")
    signature = wallet.hotkey.sign(message)
    return {
        "Content-Type": "application/json",
        "Epistula-Version": "2",
        "Epistula-Timestamp": timestamp,
        "Epistula-Uuid": request_uuid,
        "Epistula-Signed-By": wallet.hotkey.ss58_address,
        "Epistula-Signed-For": signed_for,
        "Epistula-Request-Signature": "0x" + signature.hex(),
    }


def validator_api_headers(internal_api_key: str, wallet, body: bytes, request_uuid: str = "") -> dict[str, str]:
    request_uuid = request_uuid or str(uuid.uuid4())
    if wallet is not None and hasattr(wallet, "hotkey"):
        headers = epistula_headers(wallet, wallet.hotkey.ss58_address, body, request_uuid=request_uuid)
        if internal_api_key:
            headers["X-Internal-API-Key"] = internal_api_key
        return headers
    return internal_key_headers(internal_api_key, request_uuid)


def publish_metagraph_sync(
    api_url: str,
    internal_api_key: str,
    active_miners: list[tuple[int, str]],
    netuid: int = 0,
    metagraph_block: int = 0,
    authoritative_empty: bool = False,
    wallet=None,
    timeout: int = 10,
    urlopen=None,
    coldkeys=(),
) -> bool:
    """Publish chain-active miners to the API-owned miner table.

    An empty active_miners list is only ever sent as an authoritative-empty
    sync (authoritative_empty=True with netuid and metagraph_block metadata).
    An unqualified empty list is refused here so a transient/unknown chain
    read can never deactivate every miner.

    The destination must pass ``api_transport.validate_api_url`` (HTTPS for remote
    hosts; plaintext only for a literal loopback IP; no userinfo) before any header is
    built or signed, and again immediately before the POST. A refused destination
    sends nothing. The default transport refuses HTTP redirects (the internal API key
    and Epistula headers must never be forwarded to another origin); a redirect is
    treated as a transport failure (False). Tests may inject ``urlopen``. Log lines
    never include the URL, the key or raw exception text.
    """
    if not api_url or (not internal_api_key and wallet is None):
        return False

    if not active_miners and not authoritative_empty:
        logger.warning("Refusing to publish an unqualified empty metagraph sync")
        return False

    try:
        endpoint = validated_endpoint(api_url, METAGRAPH_SYNC_PATH)
    except ApiDestinationError as exc:
        logger.warning("Refusing to publish metagraph sync: %s", exc)
        return False

    payload = metagraph_sync_body(active_miners, netuid, metagraph_block, authoritative_empty, coldkeys)
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            **validator_api_headers(internal_api_key, wallet, payload),
            "User-Agent": VALIDATOR_USER_AGENT,
        },
    )
    try:
        with send_request(request, timeout=timeout, urlopen=urlopen) as response:
            if response.status >= 300:
                logger.warning("API metagraph sync returned HTTP %s", response.status)
                return False
            return True
    except (OSError, urllib.error.URLError, ApiDestinationError) as exc:
        logger.warning("Failed to publish metagraph sync to API: %s", describe_transport_error(exc))
        return False


def resolve_burn_uid(subtensor, metagraph, netuid: int) -> int | None:
    """Return the UID of the subnet owner's registered hotkey, or None.

    Miner incentive directed to the subnet-owner/immune hotkey UID is
    withheld and burned/recycled under current Subtensor RecycleOrBurn
    semantics. That burn path is only valid when the target UID is the actual
    owner/immune hotkey; an arbitrary UID 0 receives ordinary miner incentive
    and is not a burn target. When the owner/immune hotkey UID cannot be
    proven, return None so the caller skips the fallback instead of weighting
    an arbitrary UID.
    """
    try:
        info = subtensor.get_subnet_info(netuid)
    except Exception:
        logger.warning("Unable to load subnet info for owner UID resolution; skipping burn fallback")
        return None

    hotkeys = list(getattr(metagraph, "hotkeys", None) or [])
    coldkeys = getattr(metagraph, "coldkeys", None)
    owner_coldkey = getattr(info, "owner_ss58", None)
    owner_hotkey = getattr(info, "owner_hotkey", None)

    if owner_hotkey:
        for uid, hotkey in enumerate(hotkeys):
            if hotkey == owner_hotkey:
                return uid

    if owner_coldkey and coldkeys is not None:
        for uid, coldkey in enumerate(coldkeys):
            if uid < len(hotkeys) and coldkey == owner_coldkey:
                return uid

    if owner_coldkey:
        for uid, hotkey in enumerate(hotkeys):
            if hotkey == owner_coldkey:
                return uid

    logger.warning("Owner/immune hotkey UID could not be resolved; skipping burn fallback")
    return None


def dedup_recommended_uids(resolved, coldkeys, reg_blocks):
    """Collapse multiple hotkeys per coldkey to one survivor.

    Groups (uid, weight) pairs by coldkey and keeps only the UID whose
    hotkey registered at the highest block; registration-block ties break
    deterministically by lowest UID. Returns the surviving pairs so weights
    retain their raw weights (burn remainder is applied by weights_with_burn_remainder).
    """
    if not resolved or not coldkeys or not reg_blocks:
        return resolved
    survivors_by_coldkey = {}
    for uid, weight in resolved:
        if uid >= len(coldkeys) or uid >= len(reg_blocks):
            continue
        coldkey = coldkeys[uid]
        reg_block = reg_blocks[uid]
        current = survivors_by_coldkey.get(coldkey)
        if current is None:
            survivors_by_coldkey[coldkey] = (uid, weight, reg_block)
            continue
        current_uid, _, current_block = current
        if reg_block > current_block or (reg_block == current_block and uid < current_uid):
            survivors_by_coldkey[coldkey] = (uid, weight, reg_block)
    deduped = [(uid, weight) for uid, weight, _ in survivors_by_coldkey.values()]
    logger.info(
        "Incentive dedup: %d UIDs kept (one per coldkey) from %d candidates",
        len(deduped),
        len(resolved),
    )
    return deduped


def _metagraph_registration_blocks(metagraph):
    """Return (coldkeys, block_at_registration) when the metagraph exposes
    them, else (None, None). The registration block is exposed by bittensor
    10.5.0 as metagraph.block_at_registration (list aligned to hotkeys)."""
    coldkeys = getattr(metagraph, "coldkeys", None)
    reg_blocks = getattr(metagraph, "block_at_registration", None)
    if coldkeys is None or reg_blocks is None:
        return None, None
    return coldkeys, reg_blocks


def renormalize_resolved(resolved):
    """Scale (uid, weight) pairs so weights sum to 1.0. Empty or all-zero
    input is returned unchanged (the caller's burn safety net handles it)."""
    total = sum(weight for _, weight in resolved)
    if total <= 0 or not resolved:
        return resolved
    return [(uid, weight / total) for uid, weight in resolved]



def weights_with_burn_remainder(resolved, burn_uid):
    """Keep surviving raw weights; send 1.0-sum(survivors) to burn_uid.

    Used after coldkey dedupe (and whenever the API allocation is intentionally
    under-full). Does NOT renormalize survivors to 1.0 when a burn UID is
    available. If burn_uid is None and the sum is in (0, 1), falls back to
    renormalizing survivors so the chain still receives a unit sum.
    Returns (uids, weights) lists ready for subtensor.set_weights.

    After appending the burn UID, normalize to ascending unique UIDs so the
    legacy path cannot emit a trailing burn UID (e.g. 0 after higher survivors).
    """
    from allocation import normalize_uid_weight_pairs

    if not resolved:
        if burn_uid is None:
            return [], []
        return [burn_uid], [1.0]
    uids = [uid for uid, _ in resolved]
    total_raw = sum(weight for _, weight in resolved)
    if total_raw <= 0:
        if burn_uid is None:
            return uids, [0.0 for _ in uids]
        return [burn_uid], [1.0]
    if total_raw < 1.0:
        weights = [weight for _, weight in resolved]
        if burn_uid is not None and burn_uid not in uids:
            uids = uids + [burn_uid]
            weights = weights + [1.0 - total_raw]
            uids, weights, _ = normalize_uid_weight_pairs(uids, weights)
            return list(uids), list(weights)
        # No burn placement: fail-soft renormalize so remainder is not dropped.
        return uids, [weight / total_raw for _, weight in resolved]
    return uids, [weight / total_raw for _, weight in resolved]



def run_bundle_epoch(subtensor, wallet, metagraph, env: EnvironmentVariables) -> bool:
    """Run one chain-v2 tick. The role is explicit (never inferred from a URL).

    Master publishes in [E, D); both master and slave consume once D is finalized.
    The slave path never touches the web API, the DB, or an internal API key.
    """
    cfg = BundleConfig(
        netuid=env.netuid,
        genesis_hash=env.allocation_expected_genesis_hash,
        master_hotkey=env.master_validator_hotkey_ss58,
        state_dir=env.bundle_state_dir,
        role=env.bundle_role,
        ipfs_api_url=env.bundle_ipfs_api_url,
        ipfs_rotation_enabled=getattr(env, "ipfs_rotation_enabled", False),
        gateway_urls=tuple(
            u.strip() for u in env.bundle_gateway_urls.split(",") if u.strip()
        ),
        api_url=env.recall_api_url,
        internal_api_key=env.internal_api_key,
        allow_plaintext=env.allocation_allow_plaintext,
        ipfs_allow_plaintext=bool(env.ipfs_allow_plaintext),
        ipfs_plaintext_hosts=env.ipfs_plaintext_allowed_hosts(),
        weight_loop_enabled=bool(env.weight_loop_enabled),
        v3_eligibility_scope=getattr(env, "incentive_v3_eligibility_scope", ""),
        v3_readiness_split=float(getattr(env, "incentive_v3_readiness_split", 0.40)),
        v3_burn_unearned=bool(getattr(env, "incentive_v3_burn_unearned", True)),
        v3_readiness_mode=str(getattr(env, "incentive_v3_readiness_mode", "shares")),
    )
    try:
        head = finalized_head(subtensor)
        schedule = read_schedule(subtensor, env.netuid, head=head)
    except ScheduleError as exc:
        logger.warning("bundle schedule read failed: %s", exc)
        return False
    if schedule.cutoff_finalized(head.block):
        if env.bundle_role == "master":
            master_reconcile_pointer(subtensor, cfg, wallet.hotkey.ss58_address)
        outcome = slave_consume(subtensor, metagraph, wallet, cfg)
        logger.info("bundle consume: status=%s reason=%s", outcome.status, outcome.reason)
        return outcome.status in ("applied", "pending", "verified")
    if env.bundle_role == "master":
        outcome = master_publish(subtensor, metagraph, wallet, cfg)
        logger.info(
            "bundle master publish: status=%s cid=%s reason=%s",
            outcome.status, outcome.cid, outcome.reason,
        )
        return outcome.status in ("published", "pending")
    return False


def run_chain_v2_tick(subtensor, wallet, metagraph, env: EnvironmentVariables) -> bool:
    """Run one chain-v2 tick and refresh the master metagraph registry afterwards.

    The registry refresh runs AFTER the bundle epoch so it can never delay this tick's
    pointer publication at E or reconciliation/consumption at D, and it runs even when the
    bundle epoch returns False or raises an ordinary error. A failed or uncertain snapshot
    produces no HTTP request. The bundle result/exception is preserved unchanged, so a
    registry transport failure never disturbs the bundle journal, dispatch, or the
    caller's UNKNOWN/PENDING handling.

    KeyboardInterrupt/SystemExit (shutdown) do NOT trigger the registry refresh: shutdown
    must not initiate new I/O.
    """
    try:
        bundle_result = run_bundle_epoch(subtensor, wallet, metagraph, env)
    except Exception:
        _maybe_run_weight_loop(subtensor, wallet, metagraph, env)
        _maybe_refresh_registry(subtensor, wallet, metagraph, env)
        raise
    _maybe_run_weight_loop(subtensor, wallet, metagraph, env)
    _maybe_refresh_registry(subtensor, wallet, metagraph, env)
    return bundle_result


_WEIGHT_LOOPS: dict = {}


def build_weight_loop(subtensor, wallet, metagraph, env: EnvironmentVariables):
    """One WeightLoop per hotkey (single signer). Holds retry state only, no chain data."""
    from weight_loop import (
        SubstrateReader,
        WeightLoop,
        owner_burn_vector,
        sdk_submitter,
        weight_vector_from_store,
    )

    return WeightLoop(
        reader=SubstrateReader(subtensor),
        submit=sdk_submitter(subtensor, wallet, env.netuid,
                             state_dir=env.bundle_state_dir,
                             genesis_hash=env.allocation_expected_genesis_hash),
        weights_source=lambda: weight_vector_from_store(env.bundle_state_dir, env.netuid),
        netuid=env.netuid,
        hotkey=wallet.hotkey.ss58_address,
        margin=int(env.weight_due_margin_blocks),
        bundle_grace_blocks=int(env.weight_bundle_grace_blocks),
        max_stale_blocks=int(getattr(env, "weight_max_stale_blocks", 0)),
        failclosed_policy=str(env.weight_failclosed_policy).strip().lower(),
        burn_vector=lambda: owner_burn_vector(subtensor, metagraph, env.netuid),
    )


def _maybe_run_weight_loop(subtensor, wallet, metagraph, env: EnvironmentVariables) -> None:
    """Block-clock weight cadence, independent of the bundle/epoch path."""
    if not bool(getattr(env, "weight_loop_enabled", False)):
        return
    hotkey = wallet.hotkey.ss58_address
    loop = _WEIGHT_LOOPS.get(hotkey)
    if loop is None:
        loop = _WEIGHT_LOOPS[hotkey] = build_weight_loop(subtensor, wallet, metagraph, env)
    try:
        results = loop.tick()
        for r in results:
            logger.info("weight loop: mech=%s status=%s reason=%s", r.mechid, r.status, r.reason)
    except Exception:
        logger.exception("weight loop tick failed")
        return
    try:
        from loop_status import read_miner_burned, write_loop_status

        write_loop_status(env.bundle_state_dir, loop, results, read_miner_burned(subtensor, env.netuid))
    except Exception:
        logger.warning("weight loop status write failed", exc_info=True)


def _maybe_refresh_registry(subtensor, wallet, metagraph, env: EnvironmentVariables) -> None:
    """Master-only registry refresh, best-effort (never disturbs the bundle result)."""
    if env.bundle_role != "master":
        return
    try:
        _refresh_master_registry(subtensor, wallet, metagraph, env)
    except Exception:
        logger.warning("master metagraph registry refresh failed", exc_info=True)
    try:
        import chain_economics

        chain_economics.maybe_publish(subtensor, wallet, env)
    except Exception:
        logger.warning("chain economics publish failed", exc_info=True)


def _refresh_master_registry(subtensor, wallet, metagraph, env: EnvironmentVariables) -> None:
    """Publish the validated master metagraph mapping to the web-owned registry.

    Uses a fresh finalized snapshot and the complete UID/hotkey mapping (never the
    coldkey-dedup allocation cohort). A non-publishable snapshot (wrong genesis/netuid,
    unregistered master, stale/no-op sync, or a chain read failure) produces no HTTP
    request; the web handler's qualified authoritative-empty semantics are preserved. A
    transport/auth rejection is logged (without secrets); it is never treated as success.
    """
    registry = build_registry_sync(
        subtensor,
        metagraph,
        env.netuid,
        env.allocation_expected_genesis_hash,
        wallet.hotkey.ss58_address,
        env.master_validator_hotkey_ss58,
    )
    if registry is None:
        return
    published = publish_metagraph_sync(
        env.recall_api_url,
        env.internal_api_key,
        list(registry.miners),
        netuid=registry.netuid,
        metagraph_block=registry.metagraph_block,
        authoritative_empty=registry.authoritative_empty,
        wallet=wallet,
        coldkeys=registry.coldkeys,
    )
    if not published:
        logger.warning(
            "master metagraph registry sync not published (transport or auth rejected)"
        )


async def run_chain_v2_validator(subtensor, wallet, metagraph, env: EnvironmentVariables) -> None:
    """Continuous chain-v2 poll loop.

    Master publishes pre-D, consumes post-D, and refreshes the metagraph registry after
    each tick; slave consumes post-D only and never touches the web API, the DB, or an
    internal API key. Each tick re-reads a fresh finalized head; the journal makes the
    post-D consume idempotent.
    """
    while True:
        try:
            run_chain_v2_tick(subtensor, wallet, metagraph, env)
        except KeyboardInterrupt:
            logger.info("Validator stopped by user")
            break
        except Exception:
            logger.exception("Error in chain-v2 validator loop")
        await asyncio.sleep(env.validator_poll_seconds)


async def run_validator(
    env: EnvironmentVariables,
):
    """Run the chain-v2 loop. Any other BUNDLE_MODE is unsupported."""
    if env.bundle_mode != "chain-v2":
        logger.error("BUNDLE_MODE=chain-v2 is the only supported mode; exiting")
        return
    wallet = Wallet(name=env.wallet_name, hotkey=env.hotkey_name, path=env.wallet_path or None)
    subtensor = bt.Subtensor(network=env.network)
    metagraph = bt.Metagraph(netuid=env.netuid, network=env.network)
    enforce_v3_startup_guards(subtensor, metagraph, wallet, env)
    logger.info(
        "Validator running chain-v2 role=%s; slave consumes the published bundle (chain pointer then IPFS) and does not call the web API",
        env.bundle_role,
    )
    await run_chain_v2_validator(subtensor, wallet, metagraph, env)


def enforce_v3_startup_guards(subtensor, metagraph, wallet, env) -> None:
    """Signer identity, actual genesis, permit and process-lock checks before the loop.

    Every validator runs them: score_version 11 is the only scoring path, so there is
    no mode in which a process could skip them. Distinct-hotkey validators may consume
    the same trusted master's bundle. Other validators' activity is not evidence of a
    duplicate signer.
    """
    from score_version import SUPPORTED_SCORE_VERSION
    from setter_guard import acquire_process_lock
    from validator_signer_guard import assert_v3_signer

    assert_v3_signer(subtensor, wallet.hotkey.ss58_address, env)
    acquire_process_lock(env.bundle_state_dir)
    logger.warning(
        "score_version %s active on netuid %s (genesis and signer verified)",
        SUPPORTED_SCORE_VERSION, env.netuid,
    )


def main():
    """Run the minimal Recall validator."""
    env = EnvironmentVariables()
    validate_deployment_secrets(env)
    logging.getLogger().setLevel(getattr(logging, env.log_level.upper()))
    logger.info("Starting validator: environment=%s network=%s netuid=%s", env.environment, env.network, env.netuid)
    asyncio.run(run_validator(env))


if __name__ == "__main__":
    main()
