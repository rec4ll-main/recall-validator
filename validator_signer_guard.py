"""V3 signer checks that permit independent, distinct-hotkey validators.

The configured master remains the sole trusted bundle publisher. Each consumer
must use its own registered, permitted signer. Process/state locks are enforced
separately; these chain reads do not prove distributed hotkey uniqueness.
"""

from bundle_runtime import _guard_genesis
from bundle_schedule import finalized_head
from setter_guard import SetterGuardError


def assert_v3_signer(subtensor, hotkey: str, env, *, head=None) -> None:
    """Role, genesis, registration and permit checks at one finalized head.

    ``head`` (a ``bundle_schedule.FinalizedHead``) lets a caller pin these reads to the
    same finalized block hash as its other reads; when omitted a fresh finalized head
    is read, as before.
    """
    master = env.master_validator_hotkey_ss58
    if not hotkey or not master:
        raise SetterGuardError("signer and trusted master hotkeys are required")
    if env.bundle_role == "master":
        if hotkey != master:
            raise SetterGuardError("master wallet must match the trusted master hotkey")
    elif env.bundle_role == "slave":
        if hotkey == master:
            raise SetterGuardError("slave must use a distinct hotkey from the master")
    else:
        raise SetterGuardError("unknown validator role")

    try:
        _guard_genesis(subtensor, env.allocation_expected_genesis_hash)
        head = head if head is not None else finalized_head(subtensor)

        def read(name, params):
            result = subtensor.substrate.query(
                module="SubtensorModule", storage_function=name,
                params=params, block_hash=head.hash,
            )
            return getattr(result, "value", result)

        def registered_uid(address):
            uid = read("Uids", [env.netuid, address])
            if isinstance(uid, bool) or not isinstance(uid, int) or not 0 <= uid <= 65535:
                raise SetterGuardError("signer or trusted master is not registered")
            if read("Keys", [env.netuid, uid]) != address:
                raise SetterGuardError("registered UID does not match its hotkey")
            return uid

        uid = registered_uid(hotkey)
        if hotkey != master:
            registered_uid(master)
        permits = read("ValidatorPermit", [env.netuid])
        if (not isinstance(permits, (list, tuple)) or uid >= len(permits)
                or permits[uid] is not True):
            raise SetterGuardError("signer has no validator permit")
    except SetterGuardError:
        raise
    except Exception as exc:
        raise SetterGuardError("genesis or finalized signer proof unavailable") from exc
