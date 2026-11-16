"""Bounded per-publication fetch attempts; keep polling the current pointer, not old bytes.

Availability bookkeeping is never acceptance evidence. Every attempt still passes
normal fetch, signature, binding and durable-storage checks before any new weights.
A publication gets at most ``MAX_ATTEMPTS`` attempts, spaced by finalized blocks after
the first and each counted durably before network I/O. A publication whose fetched
bytes failed verification is rejected and never fetched again.
"""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

from bundle_ipfs import IpfsError

# Finalized blocks after the first attempt at which attempts 1, 2 and 3 may start.
RETRY_OFFSETS = (0, 5, 25)
MAX_ATTEMPTS = len(RETRY_OFFSETS)
_PENDING = 'pending'
_REJECTED = 'rejected'


def _key(cfg, cid, schedule):
    scope = json.dumps([cfg.genesis_hash, cfg.netuid, cfg.master_hotkey])
    return (scope, schedule.epoch_start_block, schedule.subnet_epoch_index, cid)


def _open(cfg):
    root = Path(cfg.state_dir)
    root.mkdir(parents=True, exist_ok=True)
    return closing(sqlite3.connect(root / 'unavailable-bundle.sqlite3'))


def _begin(db):
    db.execute('PRAGMA synchronous=FULL')
    db.execute('BEGIN IMMEDIATE')
    db.execute('''CREATE TABLE IF NOT EXISTS unavailable (
        scope TEXT NOT NULL, epoch INTEGER NOT NULL, seq INTEGER NOT NULL,
        cid TEXT NOT NULL, PRIMARY KEY(scope,epoch,seq,cid))''')
    # Rows from the single-attempt version carry one spent attempt and no first
    # attempt block; that block is stamped from the finalized head on next sight.
    cols = {r[1] for r in db.execute('PRAGMA table_info(unavailable)')}
    if 'attempts' not in cols:
        db.execute('ALTER TABLE unavailable ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1')
    if 'first_block' not in cols:
        db.execute('ALTER TABLE unavailable ADD COLUMN first_block INTEGER')
    if 'state' not in cols:
        db.execute(f"ALTER TABLE unavailable ADD COLUMN state TEXT NOT NULL DEFAULT '{_PENDING}'")


def _rotated(cfg, attempt):
    """Start each attempt at the next configured gateway; never adds a source."""
    urls = tuple(getattr(cfg, 'gateway_urls', ()) or ())
    k = attempt % len(urls) if urls else 0
    return replace(cfg, gateway_urls=urls[k:] + urls[:k]) if k else cfg


def fetch_for_schedule(cfg, cid, schedule, fetch, *, head_block=None):
    """Fetch one publication within its attempt budget (``head_block`` is finalized).

    Without ``head_block`` no retry can be spaced, so only the first attempt runs.
    """
    key = _key(cfg, cid, schedule)
    try:
        with _open(cfg) as db:
            with db:
                _begin(db)
                row = db.execute('''SELECT attempts, first_block, state FROM unavailable
                    WHERE scope=? AND epoch=? AND seq=? AND cid=?''', key).fetchone()
                due = None
                if row is None:
                    attempt, first = 0, head_block
                else:
                    attempt, first, state = row
                    if state == _REJECTED:
                        raise IpfsError('publication failed verification; waiting for next publication')
                    # A head that moved on (or a stale schedule) never reopens an
                    # exhausted publication, or one older than a recorded newer one.
                    newer = db.execute('SELECT 1 FROM unavailable WHERE scope=? AND epoch>? LIMIT 1',
                                       key[:2]).fetchone()
                    if attempt >= MAX_ATTEMPTS or head_block is None or newer:
                        raise IpfsError('publication unavailable; waiting for next publication')
                    if first is None:
                        first = head_block
                        db.execute('''UPDATE unavailable SET first_block=?
                            WHERE scope=? AND epoch=? AND seq=? AND cid=?''', (first, *key))
                    due = first + RETRY_OFFSETS[attempt]
                if due is None or head_block >= due:
                    # Count the attempt durably before network I/O. A crash leaves it
                    # spent; a full disk prevents the fetch entirely.
                    db.execute('''INSERT INTO unavailable (scope, epoch, seq, cid, attempts, first_block, state)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT(scope,epoch,seq,cid)
                        DO UPDATE SET attempts=excluded.attempts''', (*key, attempt + 1, first, _PENDING))
            if due is not None and head_block < due:
                raise IpfsError(f'publication fetch retry not due until finalized block {due}')
            data = fetch(_rotated(cfg, attempt), cid)
            with db:
                db.execute('DELETE FROM unavailable WHERE scope=? AND epoch=? AND seq=? AND cid=?', key)
            return data
    except (OSError, sqlite3.Error) as exc:
        raise IpfsError('unavailable-publication bookkeeping failed; refusing fetch') from exc


def mark_rejected(cfg, cid, schedule):
    """Close a publication whose fetched bytes failed verification; never refetched."""
    key = _key(cfg, cid, schedule)
    try:
        with _open(cfg) as db:
            with db:
                _begin(db)
                db.execute('''INSERT INTO unavailable (scope, epoch, seq, cid, attempts, first_block, state)
                    VALUES (?,?,?,?,?,NULL,?) ON CONFLICT(scope,epoch,seq,cid)
                    DO UPDATE SET state=excluded.state''', (*key, MAX_ATTEMPTS, _REJECTED))
    except (OSError, sqlite3.Error) as exc:
        raise IpfsError('rejected-publication bookkeeping failed') from exc
