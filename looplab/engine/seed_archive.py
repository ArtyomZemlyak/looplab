"""Run-owned regular-file seed bytes, before mounts/overlay (doc 72 §9).

The capture consumes the very bytes the seed digest hashes, never a second source
copy. Private staging is published whole with a durable no-replace rename. Reads
verify the receipt's identity; existing corruption is never silently overwritten.
Storage failures annotate provenance and never refuse evaluation. No pinned-base
selection, scorer-closure claim, operator checkout write or automatic execution.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import tempfile

from looplab.core.atomicio import (durable_no_replace_rename, rmtree_readonly_aware,
                                  strict_atomic_write_bytes, strict_fsync, strict_fsync_parent)
from looplab.core.pathsafe import contained_member, is_reparse, resolve_settled

ARCHIVE_DIR = "base_snapshots"


def _destination_exists(exc):
    # The canonical Win32 writer raises OSError(native_code, ...), not
    # ctypes.WinError: ERROR_ALREADY_EXISTS(183) is not FileExistsError.
    return isinstance(exc, FileExistsError) or (os.name == "nt" and
        (exc.errno in (80, 183) or getattr(exc, "winerror", None) in (80, 183)))


def _matches(path, receipt, *, on_file=None):
    from looplab.engine.workspace_seed import seeded_base_revision
    observed = seeded_base_revision(path, on_file=on_file)
    return observed["complete"] and all(observed[k] == receipt.get(k)
                                        for k in ("version", "scope", "digest", "file_count", "bytes"))


def seed_archive_digest(receipt) -> str | None:
    """The recorded archive's canonical identity, or None for an unusable reference."""
    if not isinstance(receipt, dict) or receipt.get("complete") is not True:
        return None
    if (type(receipt.get("version")) is not int or receipt["version"] != 1
            or receipt.get("scope") != "seeded_editables_before_mounts_and_overlay"
            or any(type(receipt.get(k)) is not int or receipt[k] < 0 for k in ("file_count", "bytes"))):
        return None
    digest, archive = receipt.get("digest"), receipt.get("archive")
    if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(archive, dict) or type(archive.get("version")) is not int
            or archive["version"] != 1
            or archive.get("status") != "stored"
            or archive.get("path") != f"{ARCHIVE_DIR}/{digest}"):
        return None
    return digest


def verified_seed_archive(run_dir, receipt, *, on_file=None) -> Path | None:
    """Return a bounded, content-verified archive; never trust a supplied filesystem path."""
    digest = seed_archive_digest(receipt)
    if digest is None:
        return None
    try:
        root = Path(run_dir) / ARCHIVE_DIR
        path = root / digest
        if is_reparse(root.lstat()) or is_reparse(path.lstat()):
            return None
        if resolve_settled(path).parent != resolve_settled(root):
            return None
        return path if _matches(path, receipt, on_file=on_file) else None
    except OSError:
        return None


class SeedArchive:
    """One capture's staging; add() failures stay diagnostic, not partial publication."""

    def __init__(self, root):
        self.root, self.stage, self.reason = Path(root), None, None
        try:
            if not os.path.lexists(self.root):
                # The archive root itself must be durably discoverable after a
                # crash, not only the digest child renamed underneath it.
                initial = Path(tempfile.mkdtemp(prefix=".base-root-", dir=self.root.parent))
                try:
                    try:
                        durable_no_replace_rename(initial, self.root, label="seed archive root")
                    except OSError as exc:
                        if not _destination_exists(exc):
                            raise
                        strict_fsync_parent(self.root)
                finally:
                    if initial.exists():
                        rmtree_readonly_aware(initial)
            if is_reparse(self.root.lstat()) or not self.root.is_dir():
                raise OSError("unsupported archive root")
            self.root = resolve_settled(self.root)
            self.stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.root))
        except OSError:
            self.reason = "archive_storage_unavailable"

    def add(self, name, data, executable):
        if self.reason is not None:
            return
        try:
            target = contained_member(self.stage, name)
            if target is None:
                raise OSError("invalid archive member")
            # This canonical writer also publishes newly-created nested parents
            # durably (including Windows); mkdir + file fsync alone does not.
            strict_atomic_write_bytes(target, data)
            os.chmod(target, 0o600 | executable)  # retain executable bits despite umask
            with target.open("r+b") as fh:  # Windows fsync requires a write-capable handle
                strict_fsync(fh.fileno())
        except OSError:
            self.reason = "archive_storage_unavailable"

    def publish(self, receipt):
        unavailable = {"version": 1, "status": "unavailable", "path": None,
                       "reason": self.reason or "seed_identity_unavailable"}
        if not receipt["complete"] or self.reason is not None:
            return unavailable
        destination = self.root / receipt["digest"]
        try:
            if not _matches(self.stage, receipt):
                return {**unavailable, "reason": "archive_verification_failed"}
            try:
                durable_no_replace_rename(self.stage, destination, label="seed base archive")
                self.stage = None
            except OSError as exc:
                if not _destination_exists(exc):
                    raise
                if not _matches(destination, receipt):
                    return {**unavailable, "reason": "archive_conflict"}
                strict_fsync_parent(destination)  # re-establish an indeterminate POSIX rename
            return {"version": 1, "status": "stored", "reason": None,
                    "path": f"{ARCHIVE_DIR}/{receipt['digest']}"}
        except OSError:
            return {**unavailable, "reason": "archive_publication_unavailable"}

    def close(self):
        if self.stage is None:
            return
        try:
            # Only our generated staging child; never a linked/replaced directory.
            if (not is_reparse(self.stage.lstat()) and stat.S_ISDIR(self.stage.lstat().st_mode)
                    and resolve_settled(self.stage).parent == self.root):
                rmtree_readonly_aware(self.stage)
        except OSError:
            pass  # orphan staging is not a published archive and grants no evidence


def capture_seed_archive(workdir, root):
    from looplab.engine.workspace_seed import seeded_base_revision
    writer = SeedArchive(root)
    try:
        receipt = seeded_base_revision(workdir, on_file=writer.add)
        return {**receipt, "archive": writer.publish(receipt)}
    finally:
        writer.close()


def copy_seed_archive(run_dir, out_dir, receipt):
    """Copy only a verified recorded base, checking the read bytes before publication.

    Returns (archive status, member names). Never fall back to the live repo;
    changed reads and storage errors publish no new partially matching tree.
    """
    from looplab.engine.workspace_seed import seeded_base_revision
    unavailable = {"version": 1, "status": "unavailable", "path": None,
                   "reason": "archive_source_unavailable"}
    source = verified_seed_archive(run_dir, receipt)
    if source is None:
        return unavailable, []
    writer, names = SeedArchive(Path(out_dir) / ARCHIVE_DIR), []
    try:
        def add(name, data, executable):
            writer.add(name, data, executable)
            names.append(name)
        observed = seeded_base_revision(source, on_file=add)
        if not observed["complete"] or any(observed[k] != receipt[k]
                for k in ("version", "scope", "digest", "file_count", "bytes")):
            return {**unavailable, "reason": "archive_source_changed"}, []
        status = writer.publish(observed)
        return status, sorted(names) if status["status"] == "stored" else []
    finally:
        writer.close()
