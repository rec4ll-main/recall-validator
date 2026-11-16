"""One-shot registry bootstrap: one finalized chain snapshot, at most one API write.

THIS COMMAND CHANGES API STATE. It is not a dry run. With
``--confirm-registry-write`` it reads one complete finalized UID/hotkey snapshot of
the configured subnet and sends at most one authenticated
``POST /api/validator/metagraph-sync`` (the same body the master's post-tick refresh
sends), so miners registered on chain can complete API registration before any
master signing is activated. It then exits. There is no loop and no retry.

What it never does:

* construct a wallet or load a hotkey/coldkey (it has nothing to sign with);
* import ``main.py``, bundle publish/consume/dispatch, the weight loop, the v3
  runtime, the process lock or any other signing/runtime module;
* submit, sign or broadcast anything: every SDK submission entry point on its chain
  connection is replaced with a raising stub before the first read;
* create or edit files (SDK read-only mode is forced and bytecode writes are off),
  touch ``BUNDLE_STATE_DIR``, or change signer settings. Incentive v3 is the only
  profile, so there is no activation flag; ``--evaluate-activated-profile`` is
  accepted for old runbooks and has no effect.

Required: ``BUNDLE_ROLE=master``; explicit ``NETWORK``/``NETUID``/genesis pin in the
configuration that match ``--network``/``--netuid``/``--genesis-hash``;
``--master-hotkey`` equal to ``MASTER_VALIDATOR_HOTKEY_SS58`` (on the Finney genesis
it must equal the source-pinned ``MAINNET_MASTER_HOTKEY_SS58``); a real
``INTERNAL_API_KEY``; and a ``RECALL_API_URL`` that passes
``api_transport.validate_api_url`` (HTTPS, or plaintext only for a literal loopback
IP).

Refused before any request (exit 3): wrong genesis or netuid, master absent from the
finalized mapping, metagraph not synced at the finalized block, an incomplete
mapping (``SubnetworkN`` or any ``Keys`` entry at the snapshot's finalized hash
disagrees), a snapshot older than the first observed finalized head or more than
``--max-snapshot-lag-blocks`` behind the finalized head just before sending, an
empty mapping unless ``--allow-authoritative-empty``, or any chain read error.

Refused after the request (exit 4): redirects (never followed), any status other
than 200, a body that is not the handler's JSON object, or a no-op/rejected outcome:
``status`` must be ``ok`` with ``active_miners`` equal to the number of miners sent
(``ok_empty_authoritative`` with 0 for an authorised empty sync). A 2xx alone is not
success.

Run it only while no master validator process is running against this API (see
docs/MASTER.md). Output is one JSON evidence object with block/netuid/count/result
fields only: no key, URL, endpoint or hotkey list.

Exit codes: 0 applied, 2 usage/configuration refused (nothing read or sent),
3 snapshot refused (nothing sent), 4 API outcome refused or transport failure.
"""

from __future__ import annotations

import os
import sys

# Same file-safety posture as preflight.py: force the SDK's read-only settings and
# disable bytecode writes before any other import.
os.environ["READ_ONLY"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

EXIT_APPLIED = 0
EXIT_CONFIG = 2
EXIT_SNAPSHOT = 3
EXIT_API = 4

DEFAULT_MAX_SNAPSHOT_LAG_BLOCKS = 3
MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 15
_GENESIS_RE = re.compile(r"0x[0-9a-f]{64}")

# Modules this command must never import (directly or through its helpers).
FORBIDDEN_MODULES = frozenset({
    "main", "bundle_publish", "bundle_consume", "bundle_dispatch", "bundle_runtime",
    "bundle_store", "bundle_journal", "bundle_envelope", "bundle_chain", "weight_loop",
    "weight_loop_dispatch", "v3_runtime", "setter_guard",
    "score_version", "validator_signer_guard", "bittensor_wallet",
})

logger = logging.getLogger("registry_bootstrap")


class Refused(Exception):
    """A public, secret-free refusal reason with its exit code."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _PublicReasonFilter(logging.Filter):
    """Keep helper log lines secret-free: string arguments are cut at the first ':'
    (snapshot errors put raw RPC detail after it), so only the public reason stays."""

    def filter(self, record):
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            record.args = tuple(
                a if isinstance(a, (int, float)) and not isinstance(a, bool) else str(a).split(":", 1)[0]
                for a in args
            )
        return True


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="registry_bootstrap.py",
        description="One-shot, API-state-mutating registry bootstrap (see module docstring).",
    )
    p.add_argument("--confirm-registry-write", action="store_true",
                   help="required: confirms this command writes the API miner registry")
    p.add_argument("--network", required=False, help="must equal NETWORK in the configuration")
    p.add_argument("--netuid", type=int, required=False, help="must equal NETUID in the configuration")
    p.add_argument("--genesis-hash", required=False,
                   help="must equal ALLOCATION_EXPECTED_GENESIS_HASH in the configuration")
    p.add_argument("--master-hotkey", required=False,
                   help="must equal MASTER_VALIDATOR_HOTKEY_SS58 in the configuration")
    p.add_argument("--allow-authoritative-empty", action="store_true",
                   help="permit sending a qualified empty mapping (deactivates every API miner)")
    p.add_argument("--evaluate-activated-profile", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--max-snapshot-lag-blocks", type=int, default=DEFAULT_MAX_SNAPSHOT_LAG_BLOCKS,
                   help="largest allowed finalized-head advance past the snapshot (0..64)")
    return p


def _load_settings():
    from pydantic import ValidationError

    from environment import EnvironmentVariables

    try:
        return EnvironmentVariables()
    except ValidationError:
        raise Refused(EXIT_CONFIG, "configuration invalid (details suppressed; check the file locally)") from None


def _reject_unsafe_header_value(name: str, value: str) -> None:
    """Refuse header values that stdlib HTTP would reject with a secret-bearing error."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise Refused(EXIT_CONFIG, f"{name} contains control characters")


def check_configuration(args, env) -> dict:
    """Static checks; returns public facts. Raises ``Refused(EXIT_CONFIG)``."""
    from api_transport import METAGRAPH_SYNC_PATH, ApiDestinationError, validated_endpoint
    from deployment_config import validate_deployment_secrets
    from environment import FINNEY_GENESIS_HASH, MAINNET_MASTER_HOTKEY_SS58

    if env.bundle_role != "master":
        raise Refused(EXIT_CONFIG, "BUNDLE_ROLE must be master for the registry bootstrap")
    try:
        validate_deployment_secrets(env)
    except RuntimeError as exc:
        raise Refused(EXIT_CONFIG, f"configuration check failed: {exc}") from None
    fields = getattr(env, "model_fields_set", frozenset())
    if "allocation_expected_genesis_hash" not in fields:
        raise Refused(EXIT_CONFIG, "ALLOCATION_EXPECTED_GENESIS_HASH must be explicitly set")
    genesis = (env.allocation_expected_genesis_hash or "").strip().lower()
    if not _GENESIS_RE.fullmatch(genesis):
        raise Refused(EXIT_CONFIG, "ALLOCATION_EXPECTED_GENESIS_HASH is not a 0x-prefixed 32-byte hash")
    if args.network is None or args.netuid is None or args.genesis_hash is None or args.master_hotkey is None:
        raise Refused(EXIT_CONFIG, "--network, --netuid, --genesis-hash and --master-hotkey are required")
    if args.network != env.network:
        raise Refused(EXIT_CONFIG, "--network does not match NETWORK")
    if args.netuid != env.netuid:
        raise Refused(EXIT_CONFIG, "--netuid does not match NETUID")
    if args.genesis_hash.strip().lower() != genesis:
        raise Refused(EXIT_CONFIG, "--genesis-hash does not match ALLOCATION_EXPECTED_GENESIS_HASH")
    master = env.master_validator_hotkey_ss58
    if not master or args.master_hotkey != master:
        raise Refused(EXIT_CONFIG, "--master-hotkey does not match MASTER_VALIDATOR_HOTKEY_SS58")
    net = (env.network or "").strip().lower()
    if ("finney" in net or "mainnet" in net) and genesis != FINNEY_GENESIS_HASH:
        raise Refused(EXIT_CONFIG, "a mainnet network name requires the Finney genesis pin")
    source_pinned = genesis == FINNEY_GENESIS_HASH
    if source_pinned and master != MAINNET_MASTER_HOTKEY_SS58:
        raise Refused(EXIT_CONFIG, "on the Finney genesis the master must be the source-pinned Recall master")
    if not 0 <= args.max_snapshot_lag_blocks <= 64:
        raise Refused(EXIT_CONFIG, "--max-snapshot-lag-blocks must be between 0 and 64")
    try:
        endpoint = validated_endpoint(env.recall_api_url, METAGRAPH_SYNC_PATH)
    except ApiDestinationError as exc:
        raise Refused(EXIT_CONFIG, f"RECALL_API_URL refused: {exc}") from None
    # Header-bearing secrets must be free of control characters before Request
    # construction: stdlib http.client raises ValueError that echoes the value.
    _reject_unsafe_header_value("INTERNAL_API_KEY", env.internal_api_key or "")
    return {"genesis": genesis, "master": master, "endpoint": endpoint,
            "master_source_pinned": source_pinned}


def _strict_int(value) -> int | None:
    value = getattr(value, "value", value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def read_snapshot(subtensor, metagraph, env, cfg, max_lag: int):
    """One complete finalized UID/hotkey snapshot. Raises ``Refused(EXIT_SNAPSHOT)``."""
    from bundle_schedule import ScheduleError, finalized_head
    from chain_v2_registry import build_registry_sync

    try:
        head0 = finalized_head(subtensor)
    except ScheduleError:
        raise Refused(EXIT_SNAPSHOT, "finalized head unavailable") from None
    master = cfg["master"]
    registry = build_registry_sync(subtensor, metagraph, env.netuid, cfg["genesis"], master, master)
    if registry is None:
        raise Refused(EXIT_SNAPSHOT, "finalized snapshot refused (see the preceding reason)")
    block = registry.metagraph_block
    if block < head0.block:
        raise Refused(EXIT_SNAPSHOT, "snapshot is older than the observed finalized head")
    anchor_hash = subtensor.get_block_hash(block)
    if not isinstance(anchor_hash, str) or not anchor_hash.startswith("0x"):
        raise Refused(EXIT_SNAPSHOT, "snapshot block hash unavailable")

    def query(name, params):
        return subtensor.substrate.query(
            module="SubtensorModule", storage_function=name, params=params, block_hash=anchor_hash
        )

    hotkeys = list(metagraph.hotkeys)
    if _strict_int(query("SubnetworkN", [env.netuid])) != len(hotkeys):
        raise Refused(EXIT_SNAPSHOT, "snapshot is incomplete (SubnetworkN disagrees with the mapping)")
    for uid, hotkey in enumerate(hotkeys):
        value = query("Keys", [env.netuid, uid])
        if getattr(value, "value", value) != hotkey:
            raise Refused(EXIT_SNAPSHOT, "snapshot mapping disagrees with Keys at the finalized block")
    if _strict_int(query("Uids", [env.netuid, master])) != hotkeys.index(master):
        raise Refused(EXIT_SNAPSHOT, "master registration disagrees at the finalized block")
    expected = tuple((uid, hk) for uid, hk in enumerate(hotkeys) if hk != master)
    if registry.miners != expected:
        raise Refused(EXIT_SNAPSHOT, "registry mapping disagrees with the verified snapshot")
    try:
        head1 = finalized_head(subtensor)
    except ScheduleError:
        raise Refused(EXIT_SNAPSHOT, "finalized head unavailable") from None
    lag = head1.block - block
    if lag < 0 or lag > max_lag:
        raise Refused(EXIT_SNAPSHOT, "snapshot is stale relative to the finalized head")
    return registry, len(hotkeys), lag


# Applied outcomes only. Unknown / ignored / rejected server text is never echoed.
_APPLIED_STATUSES = frozenset({"ok", "ok_empty_authoritative"})


class _DuplicateJsonKey(ValueError):
    """Raised by the JSON object hook when a key repeats (stdlib would last-win)."""


def _json_object_no_duplicates(pairs):
    """``object_pairs_hook`` that refuses duplicate keys."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateJsonKey(key)
        out[key] = value
    return out


def check_outcome(status: int, raw: bytes, registry) -> dict:
    """Accept only the handler's applied outcome. Raises ``Refused(EXIT_API)``."""
    if status != 200:
        raise Refused(EXIT_API, f"API returned HTTP {status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise Refused(EXIT_API, "API response too large")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object_no_duplicates)
    except _DuplicateJsonKey:
        raise Refused(EXIT_API, "API response has duplicate JSON keys") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refused(EXIT_API, "API response is not JSON") from None
    if not isinstance(body, dict):
        raise Refused(EXIT_API, "API response is not a JSON object")
    api_status = body.get("status")
    active = body.get("active_miners")
    offline = body.get("marked_offline")
    for value in (active, offline):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise Refused(EXIT_API, "API response is malformed")
    expected_status = "ok_empty_authoritative" if registry.authoritative_empty else "ok"
    if expected_status not in _APPLIED_STATUSES or api_status != expected_status:
        # Fixed refusal: never echo arbitrary / unknown server status text.
        raise Refused(EXIT_API, "API did not apply the sync (non-applied or unknown status)")
    if active != len({hk for _, hk in registry.miners}):
        raise Refused(EXIT_API, "API applied a different miner count than was sent")
    return {"api_status": api_status, "api_active_miners": active, "api_marked_offline": offline}


def send_once(cfg, env, registry, *, urlopen=None) -> tuple[int, bytes]:
    from api_transport import internal_key_headers, metagraph_sync_body, send_request
    from validator_http import VALIDATOR_USER_AGENT

    body = metagraph_sync_body(list(registry.miners), registry.netuid,
                               registry.metagraph_block, registry.authoritative_empty,
                               registry.coldkeys)
    request = urllib.request.Request(
        cfg["endpoint"], data=body, method="POST",
        headers={**internal_key_headers(env.internal_api_key), "User-Agent": VALIDATOR_USER_AGENT},
    )
    with send_request(request, timeout=REQUEST_TIMEOUT_SECONDS, urlopen=urlopen) as response:
        return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)


def main(argv=None, *, subtensor_factory=None, metagraph_factory=None, urlopen=None) -> int:
    args = _parser().parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_PublicReasonFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    evidence = {"command": "registry_bootstrap", "result": "refused", "request_attempted": False}
    try:
        return _run(args, evidence, subtensor_factory, metagraph_factory, urlopen)
    except Refused as exc:
        evidence["reason"] = exc.reason
        print(json.dumps(evidence, sort_keys=True))
        print(f"REGISTRY BOOTSTRAP REFUSED: {exc.reason}", file=sys.stderr)
        return exc.code
    finally:
        root.removeHandler(handler)


def _run(args, evidence, subtensor_factory, metagraph_factory, urlopen) -> int:
    if not args.confirm_registry_write:
        raise Refused(EXIT_CONFIG, "--confirm-registry-write is required (this command writes API state)")
    if args.evaluate_activated_profile:
        from preflight import DEPRECATED_EVALUATE_FLAG

        print(DEPRECATED_EVALUATE_FLAG, file=sys.stderr)
    env = _load_settings()
    cfg = check_configuration(args, env)
    evidence.update({"netuid": env.netuid, "genesis_hash": cfg["genesis"],
                     "master_source_pinned": cfg["master_source_pinned"]})

    from preflight import guard_submissions

    try:
        if subtensor_factory is None or metagraph_factory is None:
            import bittensor as bt

            subtensor_factory = subtensor_factory or bt.Subtensor
            metagraph_factory = metagraph_factory or bt.Metagraph
        subtensor = subtensor_factory(network=env.network)
        guard_submissions(subtensor)
        metagraph = metagraph_factory(netuid=env.netuid, network=env.network, lite=True,
                                      sync=False, subtensor=subtensor)
        registry, uid_count, lag = read_snapshot(subtensor, metagraph, env, cfg,
                                                 args.max_snapshot_lag_blocks)
    except Refused:
        raise
    except Exception as exc:  # RPC/SDK failure: report the type only
        raise Refused(EXIT_SNAPSHOT, f"{type(exc).__name__} during chain reads") from None
    evidence.update({"finalized_block": registry.metagraph_block, "snapshot_lag_blocks": lag,
                     "uid_count": uid_count, "miner_count": len(registry.miners),
                     "authoritative_empty": registry.authoritative_empty})
    if registry.authoritative_empty and not args.allow_authoritative_empty:
        raise Refused(EXIT_SNAPSHOT, "finalized mapping has no miners; pass --allow-authoritative-empty to send it")

    from api_transport import ApiDestinationError, describe_transport_error

    evidence["request_attempted"] = True
    try:
        status, raw = send_once(cfg, env, registry, urlopen=urlopen)
    except urllib.error.HTTPError as exc:
        evidence["http_status"] = exc.code
        raise Refused(EXIT_API, f"API request failed: {describe_transport_error(exc)}") from None
    except ApiDestinationError as exc:
        evidence["request_attempted"] = False
        raise Refused(EXIT_CONFIG, f"RECALL_API_URL refused: {exc}") from None
    except (OSError, urllib.error.URLError) as exc:
        raise Refused(EXIT_API, f"API request failed: {describe_transport_error(exc)}") from None
    except Exception:
        # Unexpected failures (e.g. stdlib header ValueError) must never echo
        # secret-bearing exception text or traceback into the refusal output.
        raise Refused(EXIT_API, "API request failed: unexpected transport error") from None
    evidence["http_status"] = status
    evidence.update(check_outcome(status, raw, registry))
    evidence["result"] = "applied"
    print(json.dumps(evidence, sort_keys=True))
    print("REGISTRY BOOTSTRAP APPLIED. One API update was accepted; nothing was signed or submitted on chain.",
          file=sys.stderr)
    return EXIT_APPLIED


if __name__ == "__main__":
    sys.exit(main())
