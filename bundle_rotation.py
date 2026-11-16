"""Latest-three master pin accounting, scoped to an exclusive ownership domain.

Finalized publication replaces the retention reference; secondary availability never
holds old pins. All publisher processes must share this durable local state and lock.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from bundle_ipfs import compute_raw_cid

SCHEMA_VERSION = 3


class Rotation:
    def __init__(self, directory, identity):
        self.path = Path(directory) / 'ipfs-rotation.sqlite3'
        marker = Path(directory) / 'ipfs-rotation.initialized'
        existed = self.path.exists()
        if marker.exists() and not existed:
            raise ValueError('rotation ledger lost; refusing to recreate')
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA synchronous=FULL')
        if not existed:
            self.db.executescript('''
                CREATE TABLE identity (value TEXT NOT NULL);
                CREATE TABLE artifacts (cid TEXT PRIMARY KEY, owned INTEGER NOT NULL,
                    observed INTEGER NOT NULL DEFAULT 0, successful_epoch INTEGER,
                    removed INTEGER NOT NULL DEFAULT 0, protected INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE current (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    cid TEXT NOT NULL, epoch INTEGER NOT NULL);
                PRAGMA user_version=3;
            ''')
            self.db.execute('INSERT INTO identity VALUES (?)', (identity,))
            self.db.commit()
        if (self.db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION or
                self.db.execute('SELECT value FROM identity').fetchall() != [(identity,)]):
            self.db.close()
            raise ValueError('rotation identity/schema mismatch; explicit migration required')
        with marker.open('w') as stamp:
            stamp.write('3\n')
            stamp.flush()
            os.fsync(stamp.fileno())
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def prepare(self, data, is_pinned):
        cid = compute_raw_cid(data)
        if self.db.execute('SELECT 1 FROM artifacts WHERE cid=?', (cid,)).fetchone() is None:
            # Persist intent before pin/add; pre-existing pins are never adopted.
            self.db.execute('INSERT INTO artifacts(cid,owned) VALUES (?,?)', (cid, not is_pinned(cid)))
        # A pending re-publication of an old CID must survive cleanup of its old lifetime.
        self.db.execute('UPDATE artifacts SET removed=0, protected=1 WHERE cid=?', (cid,))
        self.db.commit()

    def observed(self, cid, epoch):
        """Called only after exact finalized-cutoff CID observation, never submit success."""
        if not self.db.execute('SELECT 1 FROM artifacts WHERE cid=?', (cid,)).fetchone():
            return  # historical/unmanaged artifacts never become ours
        current = self.db.execute('SELECT cid,epoch FROM current WHERE singleton=1').fetchone()
        if current and current[1] == epoch and current[0] != cid:
            raise ValueError('conflicting finalized publication at same cutoff')
        conflict = self.db.execute('SELECT cid FROM artifacts WHERE successful_epoch=? AND cid != ?', (epoch, cid)).fetchone()
        if conflict:
            raise ValueError('conflicting finalized publication at same cutoff')
        self.db.execute('''UPDATE artifacts SET observed=1, protected=0,
            successful_epoch=MAX(COALESCE(successful_epoch, -1), ?) WHERE cid=?''', (epoch, cid))
        self.db.execute('''INSERT INTO current VALUES (1,?,?) ON CONFLICT(singleton)
            DO UPDATE SET cid=excluded.cid, epoch=excluded.epoch WHERE excluded.epoch > current.epoch''', (cid, epoch))
        self.db.commit()

    def cleanup(self, is_pinned, unpin, verify_current):
        errors = {}
        rows = self.db.execute('''SELECT cid FROM artifacts WHERE owned=1 AND observed=1
            AND removed=0 AND protected=0
            AND cid NOT IN (SELECT cid FROM artifacts WHERE owned=1 AND observed=1
                            ORDER BY successful_epoch DESC LIMIT 3)
            AND cid != (SELECT cid FROM current WHERE singleton=1)''').fetchall()
        if not rows:
            return errors
        current = self.db.execute('SELECT cid FROM current WHERE singleton=1').fetchone()[0]
        try:
            if not is_pinned(current):
                raise RuntimeError('replacement pin is missing')
            verify_current(current)
        except Exception as exc:
            return {current: str(exc)}
        for (cid,) in rows:
            try:
                if is_pinned(cid):
                    unpin(cid)
                if is_pinned(cid):
                    raise RuntimeError('pin still present after removal')
                self.db.execute('UPDATE artifacts SET removed=1 WHERE cid=?', (cid,))
                self.db.commit()
            except Exception as exc:
                errors[cid] = str(exc)  # durable eligibility survives partial cleanup/crash
        return errors

    def close(self):
        self.db.close()


@contextmanager
def rotation_session(cfg):
    if not cfg.ipfs_rotation_enabled:
        yield None
        return
    if cfg.role != 'master':
        raise ValueError('rotation is master-only')
    directory = Path(cfg.state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'ipfs-rotation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity = json.dumps([cfg.genesis_hash, cfg.netuid, cfg.master_hotkey,
                               cfg.ipfs_api_url, 'latest-three-v3'])
        rotation = Rotation(directory, identity)
        try:
            yield rotation
        finally:
            rotation.close()
