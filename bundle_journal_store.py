"""SQLite persistence backend for the operation journal.

Owns the schema DDL, connection pragmas (WAL + synchronous FULL, URI mode=rw so a
deletion race never recreates a database), the key-encoding helpers that mirror the
table layout, fail-closed schema verification (structure + constraints + types), the
restart recovery pass, and the transactional wrapper that translates sqlite errors to
bounded journal errors. The state machine in ``bundle_journal`` builds on these
primitives and owns only the transition/conflict logic.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote

from bundle_journal_types import (
    JournalConflictError,
    JournalError,
    JournalSchemaError,
    JournalState,
    JournalStorageError,
    OperationKey,
)

SCHEMA_VERSION = "3"
PREVIOUS_SCHEMA_VERSION = "2"

# epoch/index are uint64 (up to 2**64-1), which exceeds SQLite's signed 64-bit
# INTEGER, so they are stored as canonical decimal TEXT and re-encoded identically
# on every query (see ``_encode_u64``).
_SCHEMA_SQL = """
CREATE TABLE schema_meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE operations (
    genesis_hash TEXT NOT NULL,
    netuid INTEGER NOT NULL,
    wallet_hotkey TEXT NOT NULL,
    epoch_start_block TEXT NOT NULL,
    subnet_epoch_index TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    artifact_cid TEXT NOT NULL,
    state TEXT NOT NULL,
    observation_ref TEXT,
    intent_block TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    outcome_ref TEXT,
    expiry_deadline TEXT,
    PRIMARY KEY (
        genesis_hash, netuid, wallet_hotkey,
        epoch_start_block, subnet_epoch_index, operation_kind
    )
);
"""

# v2 -> v3 adds the terminal-outcome reason and the mortality deadline (both nullable).
_MIGRATE_V2_TO_V3_SQL = (
    "ALTER TABLE operations ADD COLUMN outcome_ref TEXT",
    "ALTER TABLE operations ADD COLUMN expiry_deadline TEXT",
)

# (name, declared_type, notnull, pk_order) — the full expected structure.
_OPERATION_COLUMNS = (
    ("genesis_hash", "TEXT", 1, 1),
    ("netuid", "INTEGER", 1, 2),
    ("wallet_hotkey", "TEXT", 1, 3),
    ("epoch_start_block", "TEXT", 1, 4),
    ("subnet_epoch_index", "TEXT", 1, 5),
    ("operation_kind", "TEXT", 1, 6),
    ("artifact_digest", "TEXT", 1, 0),
    ("artifact_cid", "TEXT", 1, 0),
    ("state", "TEXT", 1, 0),
    ("observation_ref", "TEXT", 0, 0),
    ("intent_block", "TEXT", 0, 0),
    ("created_at", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
    ("outcome_ref", "TEXT", 0, 0),
    ("expiry_deadline", "TEXT", 0, 0),
)
_OPERATION_COLUMNS_V2 = _OPERATION_COLUMNS[:-2]

_SCHEMA_META_COLUMNS = (
    ("key", "TEXT", 1, 1),
    ("value", "TEXT", 1, 0),
)

# The only schema objects the journal creates. SQLite's internal primary-key
# autoindexes are required; anything else (trigger, view, custom index, extra table)
# is rejected as incompatible.
_EXPECTED_OBJECTS = frozenset(
    {
        ("table", "schema_meta"),
        ("table", "operations"),
        ("index", "sqlite_autoindex_schema_meta_1"),
        ("index", "sqlite_autoindex_operations_1"),
    }
)

KEY_WHERE = (
    "genesis_hash = ? AND netuid = ? AND wallet_hotkey = ? AND "
    "epoch_start_block = ? AND subnet_epoch_index = ? AND operation_kind = ?"
)

_T = TypeVar("_T")


def _encode_u64(value: int) -> str:
    """Canonical decimal TEXT form of a uint64 (exceeds SQLite's signed int64)."""
    return str(value)


def key_params(key: OperationKey) -> tuple[str | int, ...]:
    """Parameter tuple matching ``KEY_WHERE`` column order (epoch/index as TEXT)."""
    return (
        key.genesis_hash,
        key.netuid,
        key.wallet_hotkey,
        _encode_u64(key.epoch_start_block),
        _encode_u64(key.subnet_epoch_index),
        key.operation_kind.value,
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uri(path: Path, mode: str) -> str:
    return f"file:{quote(str(path))}?mode={mode}"


def connect(path: Path, *, mode: str = "rw") -> sqlite3.Connection:
    conn = sqlite3.connect(_uri(path, mode), uri=True, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 5000")
    except BaseException:
        conn.close()
        raise
    return conn


def fsync_parent(path: Path) -> None:
    """Best-effort directory fsync after file creation (Linux only).

    This does NOT guarantee power-loss durability; it only makes the fresh file's
    directory entry more likely to survive a power loss. Process-crash safety is
    provided by the WAL + synchronous FULL transaction itself.
    """
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def initialize(conn: sqlite3.Connection) -> None:
    """Create the schema and stamp the schema version on a fresh database."""
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


def parse_state(value: str | None) -> JournalState:
    """Fail closed on a malformed stored state value."""
    try:
        return JournalState(value)
    except ValueError:
        raise JournalSchemaError(f"corrupt state value: {value!r}") from None


def parse_block(value: str | None, name: str) -> int | None:
    """Decode a persisted nullable block-number TEXT to an int."""
    if value is None:
        return None
    try:
        block = int(value)
    except ValueError:
        raise JournalSchemaError(f"corrupt {name} value: {value!r}") from None
    if block < 0:
        raise JournalSchemaError(f"negative {name} value: {value!r}")
    return block


def parse_intent_block(value: str | None) -> int | None:
    """Decode a persisted nullable intent-block TEXT to an int (None = not dispatched)."""
    return parse_block(value, "intent_block")


def decode_u64(value: str) -> int:
    """Decode a canonical decimal TEXT uint64 (epoch/index) back to an int."""
    return int(value)


def _table_spec(
    conn: sqlite3.Connection, table: str, expected: tuple[tuple[str, str, int, int], ...]
) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    actual = tuple((r["name"], r["type"], r["notnull"], r["pk"]) for r in rows)
    if actual != expected:
        raise JournalSchemaError(f"{table} table schema drift")


def _verify_no_unexpected_objects(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT type, name FROM sqlite_master").fetchall()
    for row in rows:
        if (row["type"], row["name"]) not in _EXPECTED_OBJECTS:
            raise JournalSchemaError(
                f"unexpected schema object: {row['type']} {row['name']}"
            )


_OUTCOME_VALUES = frozenset(
    {JournalState.NOT_DISPATCHED, JournalState.FAILED, JournalState.EXPIRED}
)


def validate_rows(conn: sqlite3.Connection) -> None:
    """Fail closed on any invalid or inconsistent persisted operation row state."""
    for row in conn.execute(
        "SELECT state, observation_ref, outcome_ref, expiry_deadline FROM operations"
    ):
        state = parse_state(row["state"])
        if (state is JournalState.OBSERVED) != (row["observation_ref"] is not None):
            raise JournalSchemaError(
                f"inconsistent observation reference for state {state.value}"
            )
        if (state in _OUTCOME_VALUES) != (row["outcome_ref"] is not None):
            raise JournalSchemaError(
                f"inconsistent outcome reference for state {state.value}"
            )
        parse_block(row["expiry_deadline"], "expiry_deadline")


def _schema_version(conn: sqlite3.Connection) -> str | None:
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return None if version is None else version["value"]


def _quick_check(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA quick_check").fetchone()
    if row is None or row[0] != "ok":
        raise JournalSchemaError("journal failed quick_check")


def verify_schema(conn: sqlite3.Connection) -> None:
    """Fail closed on a corrupt, missing, or drifted schema (names, types, constraints)."""
    _quick_check(conn)
    if _schema_version(conn) != SCHEMA_VERSION:
        raise JournalSchemaError(f"schema version mismatch (expected {SCHEMA_VERSION})")
    _table_spec(conn, "schema_meta", _SCHEMA_META_COLUMNS)
    _table_spec(conn, "operations", _OPERATION_COLUMNS)
    _verify_no_unexpected_objects(conn)


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Upgrade an exact v2 journal to v3 in one transaction; anything else is untouched.

    The v2 structure is verified in full first, so a drifted or tampered v2 database
    still fails closed. Existing rows keep their state; the new columns start NULL.
    ``verify_schema`` must be called afterwards.
    """
    _quick_check(conn)
    if _schema_version(conn) != PREVIOUS_SCHEMA_VERSION:
        return
    _table_spec(conn, "schema_meta", _SCHEMA_META_COLUMNS)
    _table_spec(conn, "operations", _OPERATION_COLUMNS_V2)
    _verify_no_unexpected_objects(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _schema_version(conn) == PREVIOUS_SCHEMA_VERSION:
            for statement in _MIGRATE_V2_TO_V3_SQL:
                conn.execute(statement)
            conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (SCHEMA_VERSION,),
            )
        conn.commit()
    except BaseException:
        _rollback(conn)
        raise


def recover_intents(conn: sqlite3.Connection) -> None:
    """A restarted INTENT becomes UNKNOWN for eligibility; PENDING is preserved.

    A PENDING operation carries a known on-chain commit and is reconciled (not
    auto-retried) on the next poll, so it survives restart unchanged.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE operations SET state = ?, updated_at = ? WHERE state = ?",
            (JournalState.UNKNOWN.value, now(), JournalState.INTENT.value),
        )
        conn.commit()
    except BaseException:
        _rollback(conn)
        raise


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def close_conn(conn: sqlite3.Connection | None) -> None:
    if conn is not None:
        conn.close()


def run_txn(conn: sqlite3.Connection, fn: Callable[[], _T]) -> _T:
    """Run ``fn`` in a BEGIN IMMEDIATE transaction, translating sqlite errors.

    Exactly one writer wins the immediate lock; the transaction commits only if ``fn``
    returns normally and rolls back safely on any failure (including a failed commit).
    sqlite IntegrityError maps to JournalConflictError; other sqlite errors (locked,
    I/O) map to JournalStorageError. Prior committed intents are untouched by a failed
    transaction.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = fn()
        conn.commit()
        return result
    except JournalError:
        _rollback(conn)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(conn)
        raise JournalConflictError(f"storage conflict: {exc}") from exc
    except sqlite3.Error as exc:
        _rollback(conn)
        raise JournalStorageError(f"storage failure: {exc}") from exc
    except BaseException:
        _rollback(conn)
        raise
