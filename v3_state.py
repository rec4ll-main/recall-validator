"""Persisted v3 allocator state + per-epoch records (JSON, atomic writes).

Layout under BUNDLE_STATE_DIR/v3/weights/ (the only state namespace; the name is
kept from the release that also had a separate shadow mode, so state is reused):
  state.json                        allocator state (stages, carry, slots, ramps, last_epoch)
  epochs/<epoch_start_block>.json   per-epoch record: t_call, input digest + numeric inputs,
                                    units, burn by reason, encoded vector, mode (weights)
Records are written once per epoch (exclusive create); a second write for the same epoch
is refused so a restart reuses the stored result instead of advancing state twice.

Crash safety:
  * every write goes to a unique temp file in the same directory, is fsync'ed, then
    published atomically (os.replace for state.json; os.link for epoch records, which is an
    atomic exclusive create), and the directory is fsync'ed -- a crash leaves either the old
    file or the complete new one, never a torn record;
  * each epoch record carries `state_after`; on restart the runtime rolls state.json
    forward from the newest record if the process died between the two writes, so state
    never lags or double-advances;
  * leftover *.tmp files from a crash are ignored and cleaned up.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


MODES = ("weights",)


def _dir(state_dir: str, mode: str) -> Path:
    if mode not in MODES:
        raise ValueError(f"unknown v3 state mode {mode!r}")
    d = Path(state_dir) / "v3" / mode
    (d / "epochs").mkdir(parents=True, exist_ok=True)
    return d


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_tmp(directory: Path, prefix: str, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = _write_tmp(path.parent, "." + path.name + ".", data)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _atomic_create(path: Path, data: bytes) -> None:
    """Exclusive, atomic create: FileExistsError if `path` exists; never a torn file."""
    tmp = _write_tmp(path.parent, "." + path.name + ".", data)
    try:
        os.link(tmp, path)
    finally:
        os.unlink(tmp)
    _fsync_dir(path.parent)


def cleanup_tmp(state_dir: str, mode: str) -> int:
    d = _dir(state_dir, mode)
    n = 0
    for sub in (d, d / "epochs"):
        for f in sub.glob(".*.tmp"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(obj) -> str:
    return "0x" + hashlib.sha256(canonical(obj)).hexdigest()


def load_state(state_dir: str, mode: str) -> dict | None:
    p = _dir(state_dir, mode) / "state.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def save_state(state_dir: str, mode: str, state: dict) -> None:
    _atomic_write(_dir(state_dir, mode) / "state.json", canonical(state))


def load_epoch_record(state_dir: str, mode: str, epoch_start_block: int) -> dict | None:
    p = _dir(state_dir, mode) / "epochs" / f"{int(epoch_start_block)}.json"
    return json.loads(p.read_text()) if p.is_file() else None


def write_epoch_record(state_dir: str, mode: str, epoch_start_block: int, record: dict) -> Path:
    p = _dir(state_dir, mode) / "epochs" / f"{int(epoch_start_block)}.json"
    _atomic_create(p, canonical(record))
    return p


def latest_epoch_record(state_dir: str, mode: str) -> dict | None:
    d = _dir(state_dir, mode) / "epochs"
    epochs = sorted(int(f.stem) for f in d.glob("*.json") if f.stem.isdigit())
    return load_epoch_record(state_dir, mode, epochs[-1]) if epochs else None


def roll_forward_state(state_dir: str, mode: str) -> bool:
    """If the newest epoch record is ahead of state.json (crash between record and state
    write), restore state from the record's `state_after`. Returns True if rolled."""
    rec = latest_epoch_record(state_dir, mode)
    if rec is None or "state_after" not in rec:
        return False
    st = load_state(state_dir, mode) or {}
    last = st.get("last_epoch")
    if last is not None and int(last) >= int(rec["epoch_start_block"]):
        return False
    save_state(state_dir, mode, rec["state_after"])
    return True
