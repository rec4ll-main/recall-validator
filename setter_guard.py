"""Process locking and legacy active-peer helpers for incentive v3.

Current startup requires the exclusive BUNDLE_STATE_DIR/v3-setter.lock for the
process lifetime, plus validator_signer_guard's actual-genesis, registration and
permit checks. Distinct-hotkey validators may coexist. The active-peer exclusion
helper below is retained for legacy callers/tests; current startup does not call it.
Neither helper writes to chain.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from metagraph_arrays import MetagraphAlignmentError, as_list


class SetterGuardError(RuntimeError):
    pass


_HELD: dict[str, int] = {}


def acquire_process_lock(state_dir: str) -> int:
    path = Path(state_dir) / "v3-setter.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    if key in _HELD:
        return _HELD[key]
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise SetterGuardError("another v3 setter process holds the state-dir lock") from exc
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _HELD[key] = fd
    return fd


def release_process_lock(state_dir: str) -> None:
    key = str((Path(state_dir) / "v3-setter.lock").resolve())
    fd = _HELD.pop(key, None)
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def other_active_setters(metagraph, my_hotkey: str, current_block: int, max_age_blocks: int) -> list[str]:
    """Hotkeys (other than mine) with a validator permit whose last weight update is
    within max_age_blocks of current_block. Read-only metagraph fields only."""
    # numpy/torch arrays: never use `array or []` (ambiguous truth value) -- normalise.
    try:
        hotkeys = as_list(getattr(metagraph, "hotkeys", None), "hotkeys")
        permits = as_list(getattr(metagraph, "validator_permit", None), "validator_permit")
        last = as_list(getattr(metagraph, "last_update", None), "last_update")
    except MetagraphAlignmentError as exc:
        raise SetterGuardError(f"metagraph arrays unusable: {exc}") from None
    if not hotkeys or len(permits) != len(hotkeys) or len(last) != len(hotkeys):
        raise SetterGuardError("metagraph lacks validator_permit/last_update; cannot prove single setter")
    out = []
    for hk, permit, lu in zip(hotkeys, permits, last):
        if hk == my_hotkey or not bool(permit):
            continue
        if int(lu) > 0 and int(current_block) - int(lu) <= int(max_age_blocks):
            out.append(hk)
    return out


def assert_single_setter(metagraph, my_hotkey: str, current_block: int, max_age_blocks: int = 1080) -> None:
    others = other_active_setters(metagraph, my_hotkey, current_block, max_age_blocks)
    if others:
        raise SetterGuardError(
            f"{len(others)} other validator hotkey(s) set weights within {max_age_blocks} blocks; "
            "v3 weights mode refuses to run as a second setter"
        )
