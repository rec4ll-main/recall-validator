"""Read-only validator preflight. Run it before deliberate signing activation.

``python preflight.py`` loads the same settings as ``main.py`` (``.env`` beside this
file, overridden by process environment), loads the configured hotkey only to learn
its public SS58 address, and then performs chain READS: actual genesis, then one
finalized head whose block hash pins every other read (signer/master registration,
validator permit, tempo, weight rate limit, commit-reveal flag, version key, mechanism
count and per-mechanism LastUpdate). The unfinalized chain head is never read.

It never calls ``main.main``/``run_validator``, never publishes a bundle or pointer,
never sets weights, never acquires the runtime state-dir lock and never creates or
edits files. It forces the SDK's ``READ_ONLY=1`` (an inherited ``READ_ONLY=0`` is
overridden) and disables bytecode writes before importing anything else. Before the
first chain read it replaces every SDK submission entry point on the connection with
a function that raises, so an accidental call cannot reach the network.

Incentive v3 (score_version 11) is the only profile, so there is no activation flag
to evaluate: the checks always run against the profile the validator will run.
``--evaluate-activated-profile`` is accepted for old runbooks and has no effect.

Impossible cadence values (negative rate limit or mechanism count, malformed
LastUpdate, or LastUpdate ahead of the finalized head) fail the check. A proven
successful storage read of a valid empty/short LastUpdate vector uses the chain
default zero for its missing signer entry; RPC or decoding failures never do. Unavailable required cadence is a blocking incomplete result, not a
full activation-preflight pass. Every field is read at the same finalized hash.

Exit codes: 0 all checks passed, 2 configuration invalid, 3 a check failed.
Only public values are printed. Configuration error details are suppressed because
they can contain environment values.
"""

from __future__ import annotations

import os
import sys

# bittensor 10.5.0 ``core/settings.py`` creates ~/.bittensor/wallets and
# ~/.bittensor/miners at import time unless READ_ONLY == "1" exactly. The preflight
# must not create files, so it FORCES the SDK's read-only setting before any other
# import (an inherited READ_ONLY=0 or any other value is overridden), and it stops
# Python from writing __pycache__ bytecode into the checkout.
os.environ["READ_ONLY"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import errno  # noqa: E402
import fcntl  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

LOCK_NAME = "v3-setter.lock"

# SDK entry points that can sign or broadcast. Every bittensor 10.5.0 write helper
# ends in ``sign_and_send_extrinsic`` -> ``substrate.submit_extrinsic``; the named
# helpers the validator runtime uses are stubbed as well. Replaced with a raising
# stub on the preflight connection before any chain read.
_SUBMIT_ATTRS = (
    "set_weights",
    "commit_weights",
    "reveal_weights",
    "set_commitment",
    "set_reveal_commitment",
    "mev_submit_encrypted",
    "sign_and_send_extrinsic",
)
_SUBSTRATE_SUBMIT_ATTRS = ("submit_extrinsic", "create_signed_extrinsic")


class PreflightError(RuntimeError):
    """A preflight check failed (public, secret-free message)."""


class SubmissionRefused(RuntimeError):
    """Raised by the guard if anything tries to submit during preflight."""


def _refuse(name: str):
    def _raise(*_args, **_kwargs):
        raise SubmissionRefused(f"preflight is read-only: {name} refused")

    return _raise


def guard_submissions(subtensor) -> None:
    """Replace submission entry points on this connection with raising stubs."""
    for attr in _SUBMIT_ATTRS:
        if callable(getattr(subtensor, attr, None)):
            setattr(subtensor, attr, _refuse(attr))
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        for attr in _SUBSTRATE_SUBMIT_ATTRS:
            if callable(getattr(substrate, attr, None)):
                setattr(substrate, attr, _refuse(attr))


def probe_state_dir(state_dir: str) -> dict:
    """Inspect BUNDLE_STATE_DIR without creating, writing or keeping a lock.

    An existing lock file is opened read-only and probed with a non-blocking
    exclusive flock that is released immediately; a held lock means another
    validator process is using this state directory.
    """
    path = Path(state_dir)
    if not path.is_absolute():
        raise PreflightError("BUNDLE_STATE_DIR must be an absolute path for a managed service")
    if not path.is_dir():
        raise PreflightError("BUNDLE_STATE_DIR does not exist or is not a directory")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise PreflightError("BUNDLE_STATE_DIR is not readable and writable by this user")
    lock = path / LOCK_NAME
    lock_state = "absent"
    if lock.exists():
        fd = os.open(lock, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise PreflightError(
                        "another validator process holds the state-dir lock"
                    ) from None
                raise
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        lock_state = "present-unheld"
    entries = sum(1 for _ in path.iterdir())
    return {"absolute": True, "writable": True, "lock": lock_state, "entries": entries}


def public_endpoint(value: str) -> str:
    """Network name, or scheme://host[:port] of an endpoint URL (no userinfo/path/query)."""
    from urllib.parse import urlsplit

    if "://" not in value:
        return value
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


DEPRECATED_EVALUATE_FLAG = (
    "--evaluate-activated-profile is deprecated and has no effect: incentive v3 "
    "(score_version 11) is the only profile and is always evaluated."
)


def load_settings():
    from pydantic import ValidationError

    from environment import EnvironmentVariables

    try:
        return EnvironmentVariables()
    except ValidationError:
        raise SystemExit(2) from None


def _strict_int(value, name: str) -> int:
    from chain_cadence import CadenceReadError, uint
    try:
        return uint(value, name)
    except CadenceReadError as exc:
        raise PreflightError(str(exc)) from None


def read_finalized_fields(subtensor, netuid: int, hotkey: str, master: str, head) -> dict:
    """Registration, permit and cadence fields, every one read at ``head.hash``."""
    from bundle_schedule import PUBLICATION_CUTOFF_BLOCKS, block_clock_slot
    from chain_cadence import (CadenceReadError, last_update_for_uid, mechanism_count as checked_count,
                               mechanism_index, rate_allows_weights, read_storage)

    def read(name, params):
        try:
            return read_storage(subtensor.substrate, name, params, head.hash)
        except CadenceReadError as exc:
            raise PreflightError(str(exc)) from None

    def uid_of(address, label):
        uid = read("Uids", [netuid, address])
        if isinstance(uid, bool) or not isinstance(uid, int) or not 0 <= uid <= 65535:
            raise PreflightError(f"{label} is not registered at the finalized block")
        if read("Keys", [netuid, uid]) != address:
            raise PreflightError(f"{label} UID does not match its hotkey at the finalized block")
        return uid

    uid = uid_of(hotkey, "signer")
    master_uid = uid if hotkey == master else uid_of(master, "trusted master")
    permits = read("ValidatorPermit", [netuid])
    if not isinstance(permits, (list, tuple)) or uid >= len(permits) or permits[uid] is not True:
        raise PreflightError("signer has no validator permit at the finalized block")
    tempo = _strict_int(read("Tempo", [netuid]), "Tempo")
    if tempo <= 0:
        raise PreflightError("Tempo must be positive at the finalized block")
    rate_limit = _strict_int(read("WeightsSetRateLimit", [netuid]), "WeightsSetRateLimit")
    if rate_limit < 0:
        raise PreflightError("WeightsSetRateLimit must be non-negative at the finalized block")
    commit_reveal = read("CommitRevealWeightsEnabled", [netuid])
    if not isinstance(commit_reveal, bool):
        raise PreflightError("CommitRevealWeightsEnabled unreadable at the finalized block")
    version_key = _strict_int(read("WeightsVersionKey", [netuid]), "WeightsVersionKey")
    mcount = read("MechanismCountCurrent", [netuid])
    try:
        mechanism_count = checked_count(mcount)
    except CadenceReadError as exc:
        raise PreflightError(str(exc)) from None
    marker = read("LastMechansimStepBlock", [netuid])
    mechanisms = []
    for mechid in range(mechanism_count):
        try:
            index = mechanism_index(netuid, mechid, mechanism_count)
            row = read("LastUpdate", [index])
            last = last_update_for_uid(row, uid, head.block)
            allowed = rate_allows_weights(last, head.block, rate_limit)
        except CadenceReadError as exc:
            raise PreflightError(f"mechanism {mechid}: {exc}") from None
        mechanisms.append({
            "mechid": mechid,
            "last_update": last,
            "last_update_defaulted": uid >= len(row),
            "blocks_since_last_update": head.block - last,
            "rate_limit_allows_weights": allowed,
        })
    epoch_start, _seq = block_clock_slot(head.block, tempo)
    return {
        "signer_uid": uid,
        "signer_validator_permit": True,
        "master_uid": master_uid,
        "tempo": tempo,
        "weights_rate_limit": rate_limit,
        "commit_reveal_enabled": commit_reveal,
        "weights_version_key": version_key,
        "mechanism_count": mechanism_count,
        "mechanisms": mechanisms,
        "chain_epoch_block_telemetry": None if marker is None else marker,
        "bundle_epoch_start_block": epoch_start,
        "bundle_cutoff_block": epoch_start + PUBLICATION_CUTOFF_BLOCKS,
    }


def run_checks(env, subtensor, hotkey: str) -> dict:
    """Chain READS only, pinned to ONE finalized head. ``subtensor`` must be guarded.

    The finalized head is read once. The signer/role guard and every reported field
    use that head's block hash, so registration, permit and cadence values describe
    the same finalized state. The unfinalized chain head is never consulted.
    """
    from bundle_schedule import finalized_head
    from setter_guard import SetterGuardError
    from validator_signer_guard import assert_v3_signer

    head = finalized_head(subtensor)
    try:
        assert_v3_signer(subtensor, hotkey, env, head=head)
    except SetterGuardError as exc:
        raise PreflightError(str(exc)) from None
    fields = read_finalized_fields(
        subtensor, env.netuid, hotkey, env.master_validator_hotkey_ss58, head
    )
    return {
        "finalized_block": head.block,
        "finalized_hash": head.hash,
        "all_reads_at_finalized_hash": True,
        "genesis_matches_pin": True,
        "role": env.bundle_role,
        "signer_hotkey": hotkey,
        "master_hotkey": env.master_validator_hotkey_ss58,
        "netuid": env.netuid,
        **fields,
    }


def main(argv=None, *, wallet_factory=None, subtensor_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evaluate-activated-profile", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.evaluate_activated_profile:
        print(DEPRECATED_EVALUATE_FLAG, file=sys.stderr)

    try:
        env = load_settings()
    except SystemExit:
        print(
            "Configuration invalid. Check the protected environment file locally; "
            "values and exception details are suppressed.",
            file=sys.stderr,
        )
        return 2

    from deployment_config import validate_deployment_secrets

    try:
        validate_deployment_secrets(env)
    except RuntimeError as exc:
        print(f"Configuration check failed: {exc}", file=sys.stderr)
        return 2

    from score_version import SUPPORTED_SCORE_VERSION

    report = {
        "network": public_endpoint(env.network),
        "netuid": env.netuid,
        "role": env.bundle_role,
        "score_version": SUPPORTED_SCORE_VERSION,
    }
    try:
        report["state_dir"] = probe_state_dir(env.bundle_state_dir)

        if wallet_factory is None:
            from bittensor_wallet import Wallet as wallet_factory
        wallet = wallet_factory(
            name=env.wallet_name, hotkey=env.hotkey_name, path=env.wallet_path or None
        )
        try:
            hotkey = wallet.hotkey.ss58_address
        except Exception:
            raise PreflightError(
                "configured hotkey file could not be loaded (check WALLET_* names, path and permissions)"
            ) from None

        if subtensor_factory is None:
            import bittensor as bt

            subtensor_factory = bt.Subtensor
        subtensor = subtensor_factory(network=env.network)
        guard_submissions(subtensor)
        report.update(run_checks(env, subtensor, hotkey))
    except PreflightError as exc:
        print(json.dumps(report, indent=2, default=str))
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # chain/RPC failure: report the type, not raw details
        print(json.dumps(report, indent=2, default=str))
        print(f"PREFLIGHT FAILED: {type(exc).__name__} during chain reads", file=sys.stderr)
        return 3

    print(json.dumps(report, indent=2, default=str))
    print("READ-ONLY PREFLIGHT PASSED. Nothing was signed or submitted; no file was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
