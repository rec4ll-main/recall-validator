"""Connection-open lifecycle for the operation journal database.

Owns the create (exclusive create + schema init) and open (schema verify + restart
recovery) sequences so the state-machine module stays narrow. Each returns a configured
``sqlite3.Connection``; failures fail closed.

Also owns the **stale-restore fence** sidecar helpers: after a journal file is restored
from backup, operators call ``mark_journal_restored`` so ``open_existing`` / prepare
paths refuse dispatch until quarantine or a safe fence clear.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from bundle_journal_store import (
    close_conn,
    connect,
    fsync_parent,
    initialize,
    migrate_schema,
    recover_intents,
    validate_rows,
    verify_schema,
)
from bundle_journal_types import JournalFenceError, JournalSchemaError, JournalStorageError


def restore_fence_path(path: Path) -> Path:
    """Sidecar marker path for a restore fence (no secrets)."""
    return Path(str(path) + ".restore_fence")


def is_restore_fenced(path: Path) -> bool:
    return restore_fence_path(path).is_file()


def mark_journal_restored(path: Path, *, note: str = "restored") -> None:
    """Create a restore fence beside an existing journal file.

    Fail-closed if the journal path is missing. The note must be non-secret metadata only.
    """
    p = Path(path)
    if not p.is_file():
        raise JournalSchemaError(f"journal does not exist: {p}")
    if not isinstance(note, str) or not note or len(note) > 200 or "\n" in note or "\0" in note:
        raise ValueError("note must be a short single-line non-empty string")
    fence = restore_fence_path(p)
    payload = f"restore_fence=1\ncreated_unix={int(time.time())}\nnote={note}\n"
    fd = os.open(fence, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_parent(fence)


def clear_restore_fence_file(path: Path) -> None:
    """Remove the sidecar fence file only (caller must enforce safety gates)."""
    fence = restore_fence_path(Path(path))
    if fence.is_file():
        os.remove(fence)


def quarantine_journal(path: Path) -> Path:
    """Move journal (+ WAL/SHM if present) aside and leave no live DB at ``path``.

    Returns the quarantine destination path. Removes any restore fence at the original
    path after the move. Does not create a reset/retry API — operator must ``create_new``
    for a fresh journal.
    """
    p = Path(path)
    if not p.is_file():
        raise JournalSchemaError(f"journal does not exist: {p}")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = Path(str(p) + f".quarantine.{stamp}")
    if dest.exists():
        raise JournalStorageError(f"quarantine destination exists: {dest}")
    os.rename(p, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(p) + suffix)
        if side.is_file():
            os.rename(side, Path(str(dest) + suffix))
    fence = restore_fence_path(p)
    if fence.is_file():
        os.remove(fence)
    fsync_parent(dest)
    return dest


def create_connection(path: Path) -> sqlite3.Connection:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        raise JournalSchemaError(f"journal already exists: {path}") from None
    os.close(fd)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        initialize(conn)
    except sqlite3.Error as exc:
        close_conn(conn)
        raise JournalStorageError(f"cannot create journal: {path}") from exc
    except BaseException:
        close_conn(conn)
        raise
    fsync_parent(path)
    return conn


def open_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise JournalSchemaError(f"journal does not exist: {path}")
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        migrate_schema(conn)
        verify_schema(conn)
        validate_rows(conn)
        recover_intents(conn)
    except JournalSchemaError:
        close_conn(conn)
        raise
    except sqlite3.DatabaseError as exc:
        close_conn(conn)
        raise JournalSchemaError(f"corrupt or unreadable journal: {path}") from exc
    return conn
