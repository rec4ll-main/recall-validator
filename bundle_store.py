"""Durable immutable artifact store plus the state-dir first-boot/resume marker.

The signed envelope is the immutable artifact: it is persisted by an atomic exclusive
durable write *before* any chain/IPFS side effect, indexed by (netuid, epoch index,
epoch start block). A restart reuses the *exact* persisted bytes (hence the exact
CID) and never re-signs or overwrites. The artifact digest is the SHA-256 of the
artifact bytes — distinct from the metagraph snapshot digest — and is what the
journal records. The initialization marker separates first boot (create the journal)
from resume (the journal must already exist; a missing journal after the marker is a
state-loss failure, never a silent recreation).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

MARKER_NAME = ".bundle-initialized"


class ArtifactStoreError(Exception):
    """A missing, conflicting, or unwritable artifact."""


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    digest: str
    cid: str
    path: Path


def artifact_digest(envelope: bytes) -> str:
    """SHA-256 of the artifact bytes (``0x`` + 64 lowercase hex)."""
    return "0x" + hashlib.sha256(envelope).hexdigest()


def artifact_filename(netuid: int, subnet_epoch_index: int, epoch_start_block: int) -> str:
    return f"artifact-{netuid}-{subnet_epoch_index}-{epoch_start_block}.bundle"


def _fsync_parent(path: Path) -> None:
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


class ArtifactStore:
    """Stores one immutable signed envelope per (netuid, index, epoch)."""

    def __init__(self, state_dir: str) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, netuid: int, subnet_epoch_index: int, epoch_start_block: int) -> Path:
        return self._dir / artifact_filename(netuid, subnet_epoch_index, epoch_start_block)

    def load(self, netuid: int, subnet_epoch_index: int, epoch_start_block: int) -> bytes | None:
        path = self._path(netuid, subnet_epoch_index, epoch_start_block)
        if not path.is_file():
            return None
        return path.read_bytes()

    def store(
        self, netuid: int, subnet_epoch_index: int, epoch_start_block: int, envelope: bytes
    ) -> StoredArtifact:
        """Persist ``envelope`` durably and exclusively; reuse the exact bytes on a
        second call for the same key, and refuse a conflicting rewrite."""
        if not isinstance(envelope, bytes) or not envelope:
            raise ArtifactStoreError("artifact must be non-empty bytes")
        path = self._path(netuid, subnet_epoch_index, epoch_start_block)
        if path.is_file():
            existing = path.read_bytes()
            if existing != envelope:
                raise ArtifactStoreError("conflicting artifact for same epoch/identity")
            return StoredArtifact(digest=artifact_digest(existing), cid="", path=path)

        tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(envelope)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                os.unlink(tmp)
                existing = path.read_bytes()
                if existing != envelope:
                    raise ArtifactStoreError(
                        "conflicting artifact for same epoch/identity"
                    ) from None
                return StoredArtifact(
                    digest=artifact_digest(existing), cid="", path=path
                )
            os.unlink(tmp)
            _fsync_parent(path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return StoredArtifact(digest=artifact_digest(envelope), cid="", path=path)


def ensure_state_dir(state_dir: str) -> Path:
    """Ensure the state dir exists and return it (marker creation is separate)."""
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_initialized(state_dir: str) -> bool:
    """True when the first-boot marker exists (resume posture)."""
    return (Path(state_dir) / MARKER_NAME).is_file()


def write_marker(state_dir: str) -> None:
    """Atomically create the first-boot marker; idempotent across processes."""
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    marker = path / MARKER_NAME
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)
    _fsync_parent(marker)


VERIFIED_NAME = "verified-latest.json"


@dataclass(frozen=True, slots=True)
class VerifiedRef:
    """The newest bundle that passed full consume verification (weight-loop input)."""

    netuid: int
    as_of_block: int
    bundle_seq: int
    digest: str


def mark_verified(state_dir: str, netuid: int, as_of_block: int, bundle_seq: int, envelope: bytes) -> None:
    """Atomically record the newest verified bundle by its finalized block.

    ``bundle_seq`` is ``head // live_tempo``, not a globally monotonic counter.
    A tempo resize can decrease it, even while retaining the same as-of block.
    Equal blocks with a new sequence replace the reference only after consume has
    verified the envelope against the current schedule's complete identity.
    """
    import json

    current = latest_verified(state_dir, netuid)
    if current is not None:
        if current.as_of_block > as_of_block:
            return
        if current.as_of_block == as_of_block and current.bundle_seq == bundle_seq:
            return
    path = Path(state_dir) / VERIFIED_NAME
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps({
        "netuid": netuid, "as_of_block": as_of_block, "bundle_seq": bundle_seq,
        "digest": artifact_digest(envelope),
    }))
    os.replace(tmp, path)
    _fsync_parent(path)


def latest_verified(state_dir: str, netuid: int) -> VerifiedRef | None:
    import json

    path = Path(state_dir) / VERIFIED_NAME
    try:
        raw = json.loads(path.read_text())
        ref = VerifiedRef(int(raw["netuid"]), int(raw["as_of_block"]), int(raw["bundle_seq"]), str(raw["digest"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return ref if ref.netuid == netuid else None
