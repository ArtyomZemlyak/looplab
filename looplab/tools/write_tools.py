"""Write/edit tool provider for the assistant: create, string-edit, patch and delete files — gated by
the SAME path/secret rules as the read scout, plus a protect-list and the permission MODE.

Same `.specs()`/`.execute(name, args)` shape as `RepoScoutTools`/`RunTools`, so it drops into the
shared `agent.drive_tool_loop` via `CompositeTools`. Every mutation is:
  1. GATED — resolve within allowed roots, refuse secrets (`_pathsafe.looks_secret`), refuse
     protected paths (run event logs / graders / .git — see `perm_modes.DEFAULT_PROTECT`);
  2. AUTHORIZED by the mode — `plan` refuses, `default`/`acceptEdits` may ask (the injected
     `approver` blocks on a UI confirm-card), `auto`/`acceptEdits` apply inline;
  3. only then applied to disk. A refusal is a normal string returned to the model (never an
     exception), so the loop keeps going.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Callable, Optional

from looplab.core import _pathsafe
from looplab.tools._base import fn_spec
from looplab.tools.patch import SurfacePolicy, apply_patch as _apply_patch, gate as _gate
from looplab.tools.perm_modes import (
    DEFAULT_PROTECT, DEFAULT_PROTECT_EXCEPTIONS, authorize, default_approver)

_MAX_PREVIEW = 4000
_BACKUP_STACK_LOCK = threading.RLock()
_MAX_BACKUP_SLOT_DIGITS = 20


def _backup_stack_locked(method):
    """Serialize every process-local snapshot stack mutation, including exact check-and-restore."""
    def locked(self, *args, **kwargs):
        with _BACKUP_STACK_LOCK:
            return method(self, *args, **kwargs)
    return locked


def _valid_file_state(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"exists", "digest"}:
        return False
    if not isinstance(value.get("exists"), bool):
        return False
    digest = value.get("digest")
    return ((value["exists"] and isinstance(digest, str) and len(digest) == 64
             and all(ch in "0123456789abcdef" for ch in digest))
            or (not value["exists"] and digest is None))


def _valid_backup_slot(value) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= _MAX_BACKUP_SLOT_DIGITS
            and value.isascii() and value.isdigit()
            and (value == "0" or not value.startswith("0")))


def _valid_permission_mode(value) -> bool:
    return value is None or (type(value) is int and 0 <= value <= 0o7777)


def _valid_mode_for_state(state, mode) -> bool:
    return (_valid_file_state(state)
            and ((state["exists"] and type(mode) is int and _valid_permission_mode(mode))
                 or (not state["exists"] and mode is None)))


class FileBackups:
    """Pre-mutation snapshots so a file edit can be reverted (undo). Each mutated path gets a stack of
    `.bak` snapshots under `<dir>/<hash(path)>/`; `revert` restores (or deletes, if the file was newly
    created) the most recent one and pops it. A reversible Assistant edit must abort if this receipt
    cannot be created, rather than claim recoverability and mutate anyway."""

    def __init__(self, directory):
        self.dir = Path(directory)

    def _key(self, path) -> Path:
        return self.dir / hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:16]

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        """Replace one small receipt atomically; incomplete temp files are never stack entries."""
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temp.write_text(json.dumps(value), encoding="utf-8")
            os.replace(temp, path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _receipt_path(directory: Path, snapshot_id: str) -> Path:
        key = hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()
        return directory / f".exact-revert-{key}.json"

    @staticmethod
    def _receipt_records(directory: Path) -> list[tuple[Path, dict]]:
        """Read only well-formed exact-revert receipts; neighbouring corruption is isolated."""
        try:
            receipt_paths = list(directory.glob(".exact-revert-*.json"))
        except OSError:
            return []
        records = []
        for receipt_path in receipt_paths:
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(receipt, dict):
                continue
            slot = receipt.get("slot")
            if not _valid_backup_slot(slot):
                continue
            records.append((receipt_path, receipt))
        return records

    def _reserved_slots(self, directory: Path) -> set[str]:
        """Slots retained by durable retry receipts must not be reused by later snapshots."""
        return {
            receipt["slot"]
            for _path, receipt in self._receipt_records(directory)
            if isinstance(receipt.get("status"), str)
            and receipt.get("status") in {"restoring", "reverted"}
        }

    def _active_baks(self, directory: Path) -> list[Path]:
        """Return legacy-visible snapshots, excluding exact receipts already being/been consumed."""
        blocked_slots = self._reserved_slots(directory)
        try:
            return sorted(
                (b for b in directory.glob("*.bak")
                 if _valid_backup_slot(b.stem) and b.stem not in blocked_slots),
                key=lambda b: int(b.stem),
            )
        except OSError:
            return []

    @staticmethod
    def _cleanup_snapshot(directory: Path, slot: str,
                          snapshot_id: Optional[str] = None) -> None:
        """Best-effort cleanup after a durable exact-revert receipt hides this slot from stacks."""
        meta_path = directory / f"{slot}.meta"
        if snapshot_id is not None and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return
            if not isinstance(meta, dict) or meta.get("snapshot_id") != snapshot_id:
                return
        for path in (directory / f"{slot}.bak", directory / f"{slot}.meta"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune_receipts(self, directory: Path) -> None:
        """Bound completed idempotency receipts while retaining any entry with leftover snapshot data."""
        try:
            receipts = sorted(directory.glob(".exact-revert-*.json"),
                              key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            return
        removable = []
        valid_receipts = {path: receipt for path, receipt in self._receipt_records(directory)}
        for receipt_path in receipts:
            receipt = valid_receipts.get(receipt_path)
            if receipt is None:
                continue
            slot = receipt["slot"]
            if (receipt.get("status") == "reverted"
                    and not (directory / f"{slot}.bak").exists()
                    and not (directory / f"{slot}.meta").exists()):
                removable.append(receipt_path)
        for stale in removable[:-128]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                return

    # How many pre-images to keep per path. Snapshots used to be append-only and never pruned, so a
    # long assistant session repeatedly editing one large file grew backup_dir without limit. Undo
    # always pops the NEWEST snapshot, so capping the depth only bounds how far back undo can reach —
    # 32 is far past any realistic undo chain and costs a bounded multiple of the file's size.
    _MAX_DEPTH = 32

    def _prune(self, d: Path) -> None:
        """Drop the oldest snapshots beyond the depth cap. Never touches the newest, so `revert`
        (which takes `baks[-1]`) and `save`'s `max(index)+1` slot allocation are both unaffected.

        Deletes the `.bak` BEFORE its `.meta`, mirroring `save`'s write order: a crash between the
        two deletes leaves a lone `.meta` (invisible to revert), never an ambiguous payload entry.
        """
        try:
            reserved = self._reserved_slots(d)
            baks = sorted((b for b in d.glob("*.bak")
                           if _valid_backup_slot(b.stem) and b.stem not in reserved),
                          key=lambda b: int(b.stem))
        except OSError:
            return
        for stale in baks[:max(0, len(baks) - self._MAX_DEPTH)]:
            try:
                stale.unlink()
                (d / f"{stale.stem}.meta").unlink(missing_ok=True)
            except OSError:
                return                      # a prune failure must never fail the write it follows

    @_backup_stack_locked
    def save(self, path, *, snapshot_id: Optional[str] = None,
             expected_postimage: Optional[dict] = None,
             expected_preimage: Optional[dict] = None,
             expected_preimage_mode=None) -> bool:
        """Persist one pre-image.

        ``snapshot_id`` and ``expected_postimage`` form the opaque receipt used by the browser's
        explicit Undo action.  They are optional because patch failure cleanup and the model-facing
        ``revert_file`` tool deliberately retain the historical path-only stack contract.
        """
        if snapshot_id is not None and (not isinstance(snapshot_id, str) or not snapshot_id
                                        or not _valid_file_state(expected_postimage)):
            return False
        if expected_preimage is not None and not _valid_mode_for_state(
                expected_preimage, expected_preimage_mode):
            return False
        try:
            p = Path(path)
            d = self._key(p)
            d.mkdir(parents=True, exist_ok=True)
            self._discard_uncommitted_phantoms(d)
            # max(index)+1, NOT len(): a gap (from a revert pop or a failed save) must not reuse a
            # live slot and clobber an existing backup. Durable retry receipts reserve their slots
            # even after snapshot cleanup so an old retry can never target a newer mutation.
            existing = [int(b.stem) for b in d.glob("*.bak")
                        if _valid_backup_slot(b.stem)]
            existing.extend(int(slot) for slot in self._reserved_slots(d))
            n = (max(existing) + 1) if existing else 0
            if len(str(n)) > _MAX_BACKUP_SLOT_DIGITS:
                return False
            existed = p.is_file()
            payload = p.read_bytes() if existed else b""
            # Write the `.meta` BEFORE the `.bak`. `revert` enumerates `*.bak`, so a failed payload write
            # leaves only a lone `.meta` (invisible to the stack) and a partial save cannot consume Undo.
            preimage_state = {
                "exists": existed,
                "digest": hashlib.sha256(payload).hexdigest() if existed else None,
            }
            mode = stat.S_IMODE(p.stat().st_mode) if existed else None
            if (expected_preimage is not None
                    and (preimage_state != expected_preimage
                         or mode != expected_preimage_mode)):
                return False
            meta = {"path": str(p), "existed": existed, "preimage": preimage_state,
                    "mode": mode}
            if snapshot_id is not None:
                meta["snapshot_id"] = snapshot_id
                meta["expected_postimage"] = expected_postimage
                meta["expected_postimage_mode"] = None
                # A process crash before the mutation must not leave a phantom top-of-stack Undo.
                # This bit is flipped atomically only after the expected post-image is on disk.
                meta["mutation_committed"] = False
            meta_path = d / f"{n}.meta"
            bak_path = d / f"{n}.bak"
            suffix = secrets.token_hex(8)
            meta_temp = d / f".{n}.{suffix}.meta.tmp"
            bak_temp = d / f".{n}.{suffix}.bak.tmp"
            try:
                meta_temp.write_text(json.dumps(meta), encoding="utf-8")
                bak_temp.write_bytes(payload)
                os.replace(meta_temp, meta_path)
                os.replace(bak_temp, bak_path)
            finally:
                for temp in (meta_temp, bak_temp):
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass
            self._prune(d)                  # after the new snapshot lands, never before
            return True
        except OSError:
            return False

    @_backup_stack_locked
    def discard_exact_uncommitted(self, path, snapshot_id: str) -> bool:
        """Drop the exact snapshot when its caller proves no mutation was started.

        This is cleanup only: it never reads or changes the live target, so a concurrent external
        edit/chmod that invalidated the pre-mutation fence is preserved verbatim.
        """
        if not isinstance(snapshot_id, str) or not snapshot_id:
            return False
        p = Path(path)
        d = self._key(p)
        if not d.is_dir():
            return False
        try:
            requested_path = p.resolve()
            matches = []
            for bak in d.glob("*.bak"):
                if not _valid_backup_slot(bak.stem):
                    continue
                meta_path = d / f"{bak.stem}.meta"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    continue
                recorded_path = Path(meta.get("path", "")).resolve()
                if (recorded_path == requested_path
                        and meta.get("snapshot_id") == snapshot_id
                        and meta.get("mutation_committed") is False):
                    matches.append(bak.stem)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        if len(matches) != 1:
            return False
        self._cleanup_snapshot(d, matches[0], snapshot_id)
        return not (d / f"{matches[0]}.bak").exists()

    @staticmethod
    def _snapshot_payload_valid(meta: dict, payload: bytes) -> bool:
        preimage = meta.get("preimage")
        if not _valid_file_state(preimage):
            return False
        if preimage["exists"]:
            return hashlib.sha256(payload).hexdigest() == preimage["digest"]
        return payload == b""

    @staticmethod
    def _snapshot_mode_valid(meta: dict) -> bool:
        preimage = meta.get("preimage")
        if not _valid_file_state(preimage):
            return False
        mode = meta.get("mode")
        return _valid_mode_for_state(preimage, mode)

    def _discard_uncommitted_phantoms(self, directory: Path) -> None:
        """Drop exact snapshots proven to have crashed before their mutation became visible."""
        reserved = self._reserved_slots(directory)
        try:
            baks = list(directory.glob("*.bak"))
        except OSError:
            return
        for bak in baks:
            if not _valid_backup_slot(bak.stem) or bak.stem in reserved:
                continue
            try:
                meta = json.loads((directory / f"{bak.stem}.meta").read_text(encoding="utf-8"))
                payload = bak.read_bytes()
                if not isinstance(meta, dict) or meta.get("mutation_committed") is not False:
                    continue
                snapshot_id = meta.get("snapshot_id")
                expected = meta.get("expected_postimage")
                preimage = meta.get("preimage")
                if (not isinstance(snapshot_id, str) or not snapshot_id
                        or not _valid_file_state(expected)
                        or not self._snapshot_payload_valid(meta, payload)
                        or not self._snapshot_mode_valid(meta)):
                    continue
                recorded_path = Path(meta.get("path", "")).resolve()
            except (OSError, ValueError, TypeError, RuntimeError):
                continue
            if self._exact_state_matches(recorded_path, preimage, meta.get("mode")):
                self._cleanup_snapshot(directory, bak.stem, snapshot_id)

    @_backup_stack_locked
    def commit_exact(self, path, snapshot_id: str, expected_postimage: dict,
                     expected_postimage_mode) -> Optional[dict]:
        """Publish one exact snapshot only after its expected post-image is durably observable."""
        if (not isinstance(snapshot_id, str) or not snapshot_id
                or not _valid_mode_for_state(expected_postimage, expected_postimage_mode)):
            return None
        p = Path(path)
        d = self._key(p)
        if not d.is_dir() or not self._exact_state_matches(
                p, expected_postimage, expected_postimage_mode):
            return None
        try:
            requested_path = p.resolve()
            candidates = list(d.glob("*.bak"))
        except (OSError, RuntimeError, ValueError):
            return None
        matches: list[tuple[Path, dict]] = []
        for bak in candidates:
            if not _valid_backup_slot(bak.stem):
                continue
            try:
                meta_path = d / f"{bak.stem}.meta"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                payload = bak.read_bytes()
                if not isinstance(meta, dict):
                    continue
                recorded_path = Path(meta.get("path", "")).resolve()
            except (OSError, ValueError, TypeError, RuntimeError):
                continue
            if (recorded_path == requested_path and meta.get("snapshot_id") == snapshot_id
                    and meta.get("expected_postimage") == expected_postimage
                    and self._snapshot_payload_valid(meta, payload)
                    and self._snapshot_mode_valid(meta)):
                matches.append((meta_path, meta))
        if len(matches) != 1:
            return None
        meta_path, meta = matches[0]
        if not self._exact_state_matches(p, expected_postimage, expected_postimage_mode):
            return None
        committed = {**expected_postimage, "mode": expected_postimage_mode}
        if meta.get("mutation_committed") is True:
            return (committed
                    if meta.get("expected_postimage_mode") == expected_postimage_mode else None)
        if meta.get("mutation_committed") is not False:
            return None
        try:
            self._atomic_json(meta_path, {**meta, "mutation_committed": True,
                                         "expected_postimage_mode": expected_postimage_mode})
        except OSError:
            return None
        return committed

    @staticmethod
    def _current_postimage(path: Path) -> Optional[dict]:
        """Return the exact live file state used by a browser Undo CAS, or ``None`` for a non-file.

        A directory, unreadable file, or any other exceptional filesystem object must not compare as
        either an absent file or a matching regular file: exact Undo fails closed in those states.
        """
        try:
            if path.is_symlink():
                return None
            if not path.exists():
                return {"exists": False, "digest": None}
            if not path.is_file():
                return None
            return {"exists": True, "digest": hashlib.sha256(path.read_bytes()).hexdigest()}
        except OSError:
            return None

    @staticmethod
    def _current_mode(path: Path) -> Optional[int]:
        """Return regular-file permission bits, or ``None`` for absent/unsafe/unreadable targets."""
        try:
            if path.is_symlink() or not path.is_file():
                return None
            mode = stat.S_IMODE(path.stat().st_mode)
            return mode if _valid_permission_mode(mode) else None
        except (OSError, OverflowError, ValueError):
            return None

    @staticmethod
    def _exact_state_matches(path: Path, state: dict, mode) -> bool:
        return (_valid_mode_for_state(state, mode)
                and FileBackups._current_postimage(path) == state
                and FileBackups._current_mode(path) == mode)

    @staticmethod
    def _restore_preimage(path: Path, payload: bytes, preimage: dict,
                          expected_postimage: dict, expected_postimage_mode,
                          mode: Optional[int] = None) -> bool:
        """Stage a complete pre-image, recheck the live CAS value, then replace in one filesystem op."""
        if (not _valid_mode_for_state(preimage, mode)
                or not _valid_mode_for_state(expected_postimage, expected_postimage_mode)):
            return False
        temp = None
        try:
            if preimage["exists"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(f".{path.name}.assistant-undo-{secrets.token_hex(8)}.tmp")
                temp.write_bytes(payload)
                if type(mode) is int:
                    os.chmod(temp, mode)
                # Preparing a large pre-image can take time. Recheck immediately before replace so a
                # concurrent non-Assistant edit during staging is refused instead of overwritten.
                if not FileBackups._exact_state_matches(
                        path, expected_postimage, expected_postimage_mode):
                    return False
                os.replace(temp, path)
                temp = None
            else:
                if not FileBackups._exact_state_matches(
                        path, expected_postimage, expected_postimage_mode):
                    return False
                path.unlink()
            return FileBackups._exact_state_matches(path, preimage, mode)
        except (OSError, OverflowError, TypeError, ValueError):
            return False
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    @_backup_stack_locked
    def revert_exact(self, path, snapshot_id: str, expected_postimage: dict,
                     expected_postimage_mode, *,
                     allow_uncommitted: bool = False) -> Optional[str]:
        """Restore one exact snapshot, durably idempotent across retries and lost HTTP responses.

        ``reverted`` means this call applied the restore; ``already_reverted`` means the same opaque
        receipt completed earlier. ``None`` is a fail-closed conflict and never consumes another slot.
        """
        if not _valid_mode_for_state(expected_postimage, expected_postimage_mode):
            return None
        p = Path(path)
        d = self._key(p)
        if not d.is_dir():
            return None
        self._discard_uncommitted_phantoms(d)
        try:
            requested_path = p.resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        receipt_path = self._receipt_path(d, snapshot_id)
        receipt = None
        if receipt_path.exists():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if not isinstance(receipt, dict):
                    return None
                receipt_path_value = Path(receipt.get("path", "")).resolve()
            except (OSError, ValueError, TypeError, RuntimeError):
                return None
            if (receipt.get("snapshot_id") != snapshot_id
                    or receipt.get("expected_postimage") != expected_postimage
                    or receipt.get("expected_postimage_mode") != expected_postimage_mode
                    or receipt_path_value != requested_path
                    or not _valid_mode_for_state(
                        receipt.get("preimage"), receipt.get("preimage_mode"))
                    or not _valid_backup_slot(receipt.get("slot"))):
                return None
            if receipt.get("status") == "reverted":
                self._cleanup_snapshot(d, receipt["slot"], snapshot_id)
                self._prune_receipts(d)
                return "already_reverted"
            if receipt.get("status") != "restoring":
                return None
            if self._exact_state_matches(
                    p, receipt["preimage"], receipt["preimage_mode"]):
                completed = {**receipt, "status": "reverted"}
                try:
                    self._atomic_json(receipt_path, completed)
                except OSError:
                    pass
                self._cleanup_snapshot(d, receipt["slot"], snapshot_id)
                self._prune_receipts(d)
                return "already_reverted"
            if not self._exact_state_matches(
                    p, expected_postimage, expected_postimage_mode):
                return None
            last = d / f"{receipt['slot']}.bak"
            try:
                newer = [bak for bak in d.glob("*.bak") if _valid_backup_slot(bak.stem)
                         and int(bak.stem) > int(receipt["slot"])]
            except OSError:
                return None
            if newer or not last.is_file():
                return None
        else:
            baks = self._active_baks(d)
            try:
                raw_baks = sorted((bak for bak in d.glob("*.bak")
                                   if _valid_backup_slot(bak.stem)),
                                  key=lambda bak: int(bak.stem))
            except OSError:
                return None
            # A durable restoring receipt is a stack barrier. Never reach through it to an older
            # snapshot, even if that older receipt happens to match this request.
            if not baks or not raw_baks or baks[-1] != raw_baks[-1]:
                return None
            last = baks[-1]

        try:
            meta = json.loads((d / f"{last.stem}.meta").read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                return None
            recorded_path = Path(meta.get("path", "")).resolve()
            preimage = meta.get("preimage")
            payload = last.read_bytes()
        except (OSError, ValueError, TypeError, RuntimeError):
            return None
        if (recorded_path != requested_path
                or meta.get("snapshot_id") != snapshot_id
                or meta.get("expected_postimage") != expected_postimage
                or not self._snapshot_payload_valid(meta, payload)
                or not self._snapshot_mode_valid(meta)):
            return None
        mode = meta.get("mode")
        committed = meta.get("mutation_committed")
        recorded_postimage_mode = meta.get("expected_postimage_mode")
        if committed is True:
            if recorded_postimage_mode != expected_postimage_mode:
                return None
        elif not (allow_uncommitted and committed is False
                  and recorded_postimage_mode is None):
            return None

        current_is_preimage = self._exact_state_matches(p, preimage, mode)
        current_is_postimage = self._exact_state_matches(
            p, expected_postimage, expected_postimage_mode)
        if current_is_preimage and not current_is_postimage:
            completed = {
                "status": "reverted", "snapshot_id": snapshot_id, "path": str(requested_path),
                "slot": last.stem, "expected_postimage": expected_postimage,
                "expected_postimage_mode": expected_postimage_mode,
                "preimage": preimage, "preimage_mode": mode,
            }
            try:
                self._atomic_json(receipt_path, completed)
            except OSError:
                return None
            self._cleanup_snapshot(d, last.stem, snapshot_id)
            self._prune_receipts(d)
            return "already_reverted"
        if not current_is_postimage:
            return None

        restoring = {
            "status": "restoring", "snapshot_id": snapshot_id, "path": str(requested_path),
            "slot": last.stem, "expected_postimage": expected_postimage,
            "expected_postimage_mode": expected_postimage_mode,
            "preimage": preimage, "preimage_mode": mode,
        }
        try:
            self._atomic_json(receipt_path, restoring)
        except OSError:
            return None
        if not self._restore_preimage(
                p, payload, preimage, expected_postimage, expected_postimage_mode, mode):
            # Keep the restoring journal: a retry can distinguish an applied pre-image from a live
            # conflict even if this process lost the result after the filesystem operation.
            return None
        try:
            self._atomic_json(receipt_path, {**restoring, "status": "reverted"})
        except OSError:
            pass
        self._cleanup_snapshot(d, last.stem, snapshot_id)
        self._prune_receipts(d)
        return "reverted"

    @_backup_stack_locked
    def revert(self, path, *, discard_phantoms: bool = True) -> bool:
        p = Path(path)
        d = self._key(p)
        if not d.is_dir():
            return False
        if discard_phantoms:
            self._discard_uncommitted_phantoms(d)
        baks = self._active_baks(d)
        try:
            raw_baks = sorted((bak for bak in d.glob("*.bak")
                               if _valid_backup_slot(bak.stem)),
                              key=lambda bak: int(bak.stem))
        except OSError:
            return False
        # A failed/in-flight exact Undo owns the newest raw slot. It must block legacy traversal;
        # exposing an older snapshot here could overwrite the live file with a too-old pre-image.
        if not baks or not raw_baks or baks[-1] != raw_baks[-1]:
            return False
        last = baks[-1]
        temp = None
        try:
            meta = json.loads((d / f"{last.stem}.meta").read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                return False
            payload = last.read_bytes()
            recorded_path = Path(meta.get("path", "")).resolve()
            if recorded_path != p.resolve():
                return False
            preimage = meta.get("preimage")
            if _valid_file_state(preimage):
                if (not self._snapshot_payload_valid(meta, payload)
                        or not self._snapshot_mode_valid(meta)):
                    return False
                mode = meta.get("mode")
            elif "preimage" not in meta and isinstance(meta.get("existed"), bool):
                # Backward compatibility for snapshots persisted before content digests were added.
                # Their payload was the sole historical source of truth; retain that behavior while
                # still requiring a valid object, exact path and empty payload for an absent file.
                if not meta["existed"] and payload:
                    return False
                preimage = {"exists": meta["existed"],
                            "digest": hashlib.sha256(payload).hexdigest()
                            if meta["existed"] else None}
                # Old-format snapshots carried no mode. Match the historical in-place write behavior:
                # preserve the live file's current permission bits when it still exists.
                mode = (stat.S_IMODE(p.stat().st_mode)
                        if meta["existed"] and p.is_file() and not p.is_symlink() else None)
            else:
                return False
            if not _valid_permission_mode(mode):
                return False
            if preimage["exists"]:
                p.parent.mkdir(parents=True, exist_ok=True)
                temp = p.with_name(f".{p.name}.assistant-revert-{secrets.token_hex(8)}.tmp")
                temp.write_bytes(payload)
                if type(mode) is int:
                    os.chmod(temp, mode)
                os.replace(temp, p)
                temp = None
            elif p.exists() or p.is_symlink():
                if p.is_dir() and not p.is_symlink():
                    return False
                p.unlink()
            last.unlink(missing_ok=True)
            (d / f"{last.stem}.meta").unlink(missing_ok=True)
            return True
        except (OSError, OverflowError, ValueError, TypeError, RuntimeError):
            return False
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass


def _diff(path: str, old: str, new: str) -> str:
    d = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             fromfile=f"a/{path}", tofile=f"b/{path}")
    return "".join(d)[:_MAX_PREVIEW]


def _write_text_bytes(text: str) -> bytes:
    """Bytes emitted by ``Path.write_text(..., encoding='utf-8')`` on this platform."""
    return text.replace("\n", os.linesep).encode("utf-8")


def _atomic_replace_text(path: Path, text: str, preserve_mode,
                         expected_preimage: dict, expected_preimage_mode) -> int:
    """Publish text with a mode chosen before the target pathname becomes externally visible.

    Existing writes preserve the exact pre-mutation mode. New files inherit the process' ordinary
    create/umask mode on an unguessable sibling temp; that mode is recorded before ``os.replace``.
    A later chmod therefore cannot be absorbed into the Assistant recovery receipt.
    """
    temp = path.with_name(f".{path.name}.assistant-write-{secrets.token_hex(8)}.tmp")
    fd = None
    published = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(temp, flags, 0o666)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(_write_text_bytes(text))
        if type(preserve_mode) is int:
            os.chmod(temp, preserve_mode)
        intended_mode = stat.S_IMODE(temp.stat().st_mode)
        if type(intended_mode) is not int or not 0 <= intended_mode <= 0o7777:
            raise OSError("invalid file mode after write staging")
        # Staging can take arbitrarily long for a large file. Recheck immediately before publish so
        # an external edit/chmod/create in that window is preserved instead of replaced.
        if not FileBackups._exact_state_matches(
                path, expected_preimage, expected_preimage_mode):
            raise OSError("target changed during write staging")
        os.replace(temp, path)
        published = True
        return intended_mode
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not published:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


class WriteTools:
    def __init__(self, roots, mode: str = "plan", protect: Optional[list] = None,
                 approver: Optional[Callable[[dict], str]] = None, repo_root=None, backup_dir=None):
        self._roots = _pathsafe.resolve_roots(roots)
        self.mode = mode
        self.protect = list(protect if protect is not None else DEFAULT_PROTECT)
        # F11: only the DEFAULT protect list carries the broad grader globs, so its migration-script
        # exceptions apply only here — an operator/manifest that passes its OWN protect list means it
        # exactly, and gets no exception override.
        self._protect_exceptions = None if protect is not None else DEFAULT_PROTECT_EXCEPTIONS
        self.approver = approver or default_approver
        self.repo_root = Path(repo_root).resolve() if repo_root else (self._roots[0] if self._roots else Path.cwd())
        self.applied: list[dict] = []
        self.backups = FileBackups(backup_dir) if backup_dir else None

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("write_file",
                     "Create or OVERWRITE a text file with the given content (within the allowed "
                     "roots; refused for protected/secret paths). Prefer edit_file for small changes.",
                     {"path": {"type": "string"}, "content": {"type": "string"}},
                     ["path", "content"]),
            fn_spec("edit_file",
                     "Replace the SINGLE occurrence of old_str with new_str in a file (exact match; "
                     "errors if old_str is missing or appears more than once — add surrounding context "
                     "to disambiguate). Read the file first.",
                     {"path": {"type": "string"}, "old_str": {"type": "string"},
                      "new_str": {"type": "string"}}, ["path", "old_str", "new_str"]),
            fn_spec("apply_patch",
                     "Apply a unified git diff (a/… b/… headers) to the repo — for multi-file or "
                     "surgical changes. Gated + `git apply --check`ed before it touches disk.",
                     {"diff": {"type": "string"}}, ["diff"]),
            fn_spec("delete_file",
                     "Delete a file (within the allowed roots; refused for protected/secret paths).",
                     {"path": {"type": "string"}}, ["path"]),
            fn_spec("revert_file",
                     "Undo your most recent change to a file (restore the pre-edit snapshot).",
                     {"path": {"type": "string"}}, ["path"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "write_file":
                return self._write(args.get("path", ""), args.get("content", ""))
            if name == "edit_file":
                return self._edit(args.get("path", ""), args.get("old_str", ""), args.get("new_str", ""))
            if name == "apply_patch":
                return self._patch(args.get("diff", ""))
            if name == "delete_file":
                return self._delete(args.get("path", ""))
            if name == "revert_file":
                return self._revert_tool(args.get("path", ""))
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - tools are advisory; never crash the loop
            return f"(error: {e})"

    # --- gating -------------------------------------------------------------
    def _rel(self, p: Path) -> str:
        for r in self._roots:
            try:
                return p.relative_to(r).as_posix()
            except ValueError:
                continue
        return p.name

    def _protected(self, rel: str) -> bool:
        # SurfacePolicy in protect-only form: surface=None (containment is enforced by
        # resolve_within on the resolved roots, not globs) and check_escapes=False (a traversal
        # can't survive resolve_within). Built per call so a live mutation of `self.protect`
        # keeps taking effect, exactly like the old inline loop.
        policy = SurfacePolicy(None, self.protect, check_escapes=False,
                               allow_exceptions=self._protect_exceptions)
        return policy.check(rel) == SurfacePolicy.PROTECTED

    def _check(self, path: str):
        """Return (resolved_path, rel, None) or (None, None, refusal_string)."""
        p = _pathsafe.resolve_within(self._roots, path)
        if p is None:
            return None, None, f"(refused: {path} is outside the allowed roots)"
        rel = self._rel(p)
        # Secret check on the ROOT-RELATIVE path, not the absolute one: otherwise a secret-named
        # component in the ROOT prefix (e.g. a workspace under /srv/.docker/… or ~/.config/…) would
        # falsely refuse EVERY file under it. A real secret inside the workspace (`.env`, `.ssh/…`)
        # still matches on the relative path.
        if _pathsafe.looks_secret(Path(rel)):
            return None, None, f"(refused: {p.name} looks like a secret/credential — not writable)"
        if self._protected(rel):
            return None, None, f"(refused: {rel} is protected — run-integrity/grader/.git files are read-only)"
        return p, rel, None

    def _authorize(self, tool_kind: str, action: dict) -> Optional[str]:
        """None => proceed; a string => the refusal/declined message to return to the model."""
        # The approver's verdict vocabulary ("allow_once"/"allow_always"/"deny") is the permission-
        # decision wire protocol named in looplab/serve/protocol.py (PERM_*). It is string-matched in
        # `perm_modes` rather than imported because tools must never import serve (layering).
        return authorize(
            self.mode, self.approver, action,
            denied=(f"(plan mode is read-only — I can't {action.get('verb', 'do that')}. "
                    "Switch the assistant to default/acceptEdits/auto to apply changes.)"),
            declined=str(action.get("label", "change")))

    # --- mutations ----------------------------------------------------------
    def _save_undo_receipt(self, path: Path, action: dict, expected_postimage: dict,
                           preimage_state: dict, preimage_mode) -> bool:
        """Save and attach the exact receipt consumed by the browser's user-clicked Undo."""
        if not self.backups:
            return True
        recovery_id = secrets.token_hex(16)
        if not self.backups.save(
                path, snapshot_id=recovery_id, expected_postimage=expected_postimage,
                expected_preimage=preimage_state,
                expected_preimage_mode=preimage_mode):
            return False
        action["recovery_id"] = recovery_id
        action["recovery_postimage_exists"] = expected_postimage["exists"]
        action["recovery_postimage_digest"] = expected_postimage["digest"]
        return True

    def _apply_recoverable_mutation(self, path: Path, action: dict, preimage_state: dict,
                                    preimage_mode, mutation: Callable[[], Optional[int]],
                                    exact_postimage: Optional[dict] = None) -> Optional[str]:
        """Revalidate, snapshot, mutate, and publish one action under the same process-local lock."""
        with _BACKUP_STACK_LOCK:
            if not FileBackups._exact_state_matches(path, preimage_state, preimage_mode):
                return "(refused: the file changed before the approved operation; retry with its current contents)"
            if self.backups:
                saved = (self._save_undo_receipt(
                    path, action, exact_postimage, preimage_state, preimage_mode)
                    if exact_postimage is not None else self.backups.save(
                        path, expected_preimage=preimage_state,
                        expected_preimage_mode=preimage_mode))
                if not saved:
                    return "(refused: could not create a recovery snapshot; no file change was applied)"
            if (self.backups and exact_postimage is not None
                    and not FileBackups._exact_state_matches(
                        path, preimage_state, preimage_mode)):
                recovery_id = action.get("recovery_id")
                if isinstance(recovery_id, str):
                    self.backups.discard_exact_uncommitted(path, recovery_id)
                for key in ("recovery_id", "recovery_postimage_exists",
                            "recovery_postimage_digest"):
                    action.pop(key, None)
                return "(refused: the file changed before the approved operation; retry with its current contents)"
            try:
                mutation_postimage_mode = mutation()
            except Exception:
                # Exact write/edit mutations stage a sibling and publish with one replace. An
                # exception means publication never happened, so remove only their private snapshot;
                # restoring it could overwrite a concurrent external chmod. Legacy mutations retain
                # their historical rollback behavior.
                if self.backups:
                    try:
                        recovery_id = action.get("recovery_id")
                        if exact_postimage is not None and isinstance(recovery_id, str):
                            self.backups.discard_exact_uncommitted(path, recovery_id)
                        else:
                            # Phantom cleanup is disabled: traversing to an older entry here could
                            # undo an unrelated prior mutation instead of this failed operation.
                            self.backups.revert(path, discard_phantoms=False)
                    except OSError:
                        pass
                raise
            if self.backups and exact_postimage is not None:
                recovery_id = action.get("recovery_id")
                committed_postimage = (self.backups.commit_exact(
                    path, recovery_id, exact_postimage, mutation_postimage_mode)
                    if (isinstance(recovery_id, str)
                        and _valid_mode_for_state(
                            exact_postimage, mutation_postimage_mode)) else None)
                if committed_postimage is None:
                    # Metadata publication failed after the mutation. Roll back only through the
                    # exact post-image CAS; a concurrent external change must never be overwritten.
                    rolled_back = (isinstance(recovery_id, str)
                                   and _valid_mode_for_state(
                                       exact_postimage, mutation_postimage_mode)
                                   and self.backups.revert_exact(
                                       path, recovery_id, exact_postimage,
                                       mutation_postimage_mode,
                                       allow_uncommitted=True) is not None)
                    if rolled_back:
                        return ("(refused: could not finalize the recovery receipt; "
                                "the file change was rolled back)")
                    return ("(refused: the file changed while its recovery receipt was being "
                            "finalized; inspect the current contents before retrying)")
                action["recovery_postimage_mode"] = committed_postimage["mode"]
            self.applied.append(action)
        return None

    def _write(self, path: str, content: str) -> str:
        p, rel, err = self._check(path)
        if err:
            return err
        if p.exists() and p.is_dir():
            return f"(refused: {rel} is a directory)"
        preimage_state = FileBackups._current_postimage(p)
        preimage_mode = FileBackups._current_mode(p)
        if not _valid_mode_for_state(preimage_state, preimage_mode):
            return f"(refused: {rel} is not a readable regular file target)"
        old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        preview = _diff(rel, old, content)
        expected_postimage = {
            "exists": True, "digest": hashlib.sha256(_write_text_bytes(content)).hexdigest(),
        }
        if preimage_state == expected_postimage:
            return f"(no change: {rel} already has the requested contents)"
        action = {"tool": "write_file", "tool_kind": "write", "path": rel, "verb": f"write {rel}",
                  "label": f"{'overwrite' if old else 'create'} {rel}", "preview": preview,
                  "abs_path": str(p), "recovery_available": self.backups is not None, "scope": {
                      "path": rel, "existed": p.exists(),
                      "preimage_digest": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                      "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                      "recovery_available": self.backups is not None,
                  }}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        # CODEX AGENT: containment was proved on a resolved pathname before approval, but the path is
        # reopened here by name. A concurrent symlink/junction ancestor swap can redirect the approved
        # write outside the allowed root; use descriptor-relative no-follow opens and revalidate the
        # final file identity immediately before the atomic replacement.
        def apply_write() -> int:
            p.parent.mkdir(parents=True, exist_ok=True)
            return _atomic_replace_text(
                p, content, preimage_mode, preimage_state, preimage_mode)
        failure = self._apply_recoverable_mutation(
            p, action, preimage_state, preimage_mode, apply_write, expected_postimage)
        if failure:
            return failure
        return f"(wrote {rel}, {len(content)} bytes)"

    def _edit(self, path: str, old_str: str, new_str: str) -> str:
        # Deliberately NOT tools/edit_match.py's apply_search_replace: the assistant's edit_file is
        # EXACT-match only (its own arg names + error strings are part of the tool contract here),
        # and adopting the repo developer's whitespace-tolerant fallback would silently change what
        # this tool applies. Keep the two matchers' semantics distinct on purpose.
        p, rel, err = self._check(path)
        if err:
            return err
        if not p.is_file():
            return f"(no such file: {rel})"
        if not old_str:
            return "(edit_file needs a non-empty old_str; use write_file to create a file)"
        preimage_state = FileBackups._current_postimage(p)
        preimage_mode = FileBackups._current_mode(p)
        if not _valid_mode_for_state(preimage_state, preimage_mode):
            return f"(refused: {rel} is not a readable regular file target)"
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(old_str)
        if n == 0:
            return f"(old_str not found in {rel} — read the file and copy the exact text)"
        if n > 1:
            return f"(old_str appears {n} times in {rel} — add surrounding context so it's unique)"
        new_text = text.replace(old_str, new_str, 1)
        preview = _diff(rel, text, new_text)
        expected_postimage = {
            "exists": True, "digest": hashlib.sha256(_write_text_bytes(new_text)).hexdigest(),
        }
        if preimage_state == expected_postimage:
            return f"(no change: {rel} already has the requested contents)"
        action = {"tool": "edit_file", "tool_kind": "write", "path": rel, "verb": f"edit {rel}",
                  "label": f"edit {rel}", "preview": preview, "abs_path": str(p),
                  "recovery_available": self.backups is not None, "scope": {
                      "path": rel,
                      "preimage_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                      "old_digest": hashlib.sha256(old_str.encode("utf-8")).hexdigest(),
                      "new_digest": hashlib.sha256(new_str.encode("utf-8")).hexdigest(),
                      "result_digest": hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
                      "recovery_available": self.backups is not None,
                  }}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        failure = self._apply_recoverable_mutation(
            p, action, preimage_state, preimage_mode,
            lambda: _atomic_replace_text(
                p, new_text, preimage_mode, preimage_state, preimage_mode),
            expected_postimage)
        if failure:
            return failure
        return f"(edited {rel})"

    def _patch(self, diff: str) -> str:
        if not diff.strip():
            return "(empty diff)"
        allow = ["**/*"]
        g = _gate(diff, allow, self.protect,
                  allow_exceptions=self._protect_exceptions)
        if not g["ok"]:
            if g["rejected"]:
                return f"(refused: out-of-surface/protected paths: {', '.join(g['rejected'])})"
            return "(empty/unparseable patch)"
        # The surface gate doesn't know about credential files (DEFAULT_PROTECT has no secret patterns),
        # so apply the SAME secret guard the write/edit/delete paths enforce — a diff must not be able
        # to create/overwrite an .env / id_rsa / *.pem that write_file would refuse.
        secret = [rp for rp in g["paths"] if _pathsafe.looks_secret(Path(rp))]
        if secret:
            return f"(refused: patch touches secret/credential paths: {', '.join(secret)})"
        def capture_preimages() -> list[dict]:
            captured = []
            for rp in sorted(g["paths"]):
                target = self.repo_root / rp
                existed = target.is_file()
                payload = target.read_bytes() if existed else b""
                captured.append({"path": rp, "existed": existed,
                                 "digest": hashlib.sha256(payload).hexdigest()})
            return captured

        try:
            preimages = capture_preimages()
        except OSError:
            return "(refused: could not read every patch target safely)"
        preimage_digest = hashlib.sha256(json.dumps(
            preimages, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        action = {"tool": "apply_patch", "tool_kind": "write",
                  "label": f"apply patch ({len(g['paths'])} file(s))", "verb": "apply this patch",
                  "preview": diff[:_MAX_PREVIEW], "paths": g["paths"],
                  "recovery_available": self.backups is not None, "scope": {
                      "paths": g["paths"],
                      "preimage_digest": preimage_digest,
                      "diff_digest": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                      "recovery_available": self.backups is not None,
                  }}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        # Snapshots must be taken from the PRE-image, so they are pushed before the apply. But a
        # snapshot that outlives a patch which never landed is a phantom: it sits on top of any real
        # snapshot for the same file, so a later user-clicked `revert` of an EARLIER edit pops the
        # phantom (restoring the unchanged current bytes) instead of undoing that edit — a silently
        # broken undo. `git apply` is all-or-nothing (checked dry-run first), so on any bail the files
        # are untouched and reverting each saved path is a clean pop that discards only the phantom.
        with _BACKUP_STACK_LOCK:
            try:
                if capture_preimages() != preimages:
                    return ("(refused: a patch target changed before the approved operation; "
                            "retry with its current contents)")
            except OSError:
                return "(refused: could not re-read every patch target safely)"
            saved: list[str] = []
            if self.backups:
                for rp in g["paths"]:
                    if self.backups.save(self.repo_root / rp):
                        saved.append(rp)
                    else:
                        for done in reversed(saved):
                            self.backups.revert(self.repo_root / done)
                        return ("(refused: could not create every recovery snapshot; "
                                "no patch was applied)")
            res = _apply_patch(
                diff, str(self.repo_root), allow, self.protect,
                allow_exceptions=self._protect_exceptions,
            )
            if not res.get("applied"):
                for rp in reversed(saved):
                    self.backups.revert(self.repo_root / rp)
                return f"(patch failed: {res.get('error', 'unknown')})"
            self.applied.append(action)
        return f"(applied patch to {', '.join(res.get('paths', []))})"

    def _delete(self, path: str) -> str:
        p, rel, err = self._check(path)
        if err:
            return err
        if not p.exists():
            return f"(no such file: {rel})"
        if p.is_dir():
            return f"(refused: {rel} is a directory — delete files, not directories)"
        preimage_state = FileBackups._current_postimage(p)
        preimage_mode = FileBackups._current_mode(p)
        if not _valid_mode_for_state(preimage_state, preimage_mode):
            return f"(refused: {rel} is not a readable regular file target)"
        payload = p.read_bytes()
        action = {"tool": "delete_file", "tool_kind": "write", "path": rel, "verb": f"delete {rel}",
                  "label": f"delete {rel}", "preview": f"delete {rel}", "abs_path": str(p),
                  "recovery_available": self.backups is not None, "scope": {
                      "path": rel, "preimage_digest": hashlib.sha256(payload).hexdigest(),
                      "recovery_available": self.backups is not None,
                  }}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        # Absence has no observable generation: create-then-delete by another process is an ABA state
        # that cannot be proven exact. Keep legacy recovery for model rollback, but do not mint the
        # browser receipt that would offer a misleading one-click resurrection.
        failure = self._apply_recoverable_mutation(
            p, action, preimage_state, preimage_mode, p.unlink, exact_postimage=None)
        if failure:
            return failure
        return f"(deleted {rel})"

    def _revert_tool(self, path: str) -> str:
        """The MODEL-invocable revert: same disk mutation as `revert`, but gated by the permission
        mode/approver like every other mutation (a revert overwrites the file from a snapshot —
        possibly clobbering manual fixes the user made since) and recorded in `applied` so the turn
        shows it. The bare `revert` below stays un-gated for the server's explicit user-clicked undo."""
        p, rel, err = self._check(path)
        if err:
            return err
        if not self.backups:
            return "(no snapshots available to revert)"
        # No abs_path in the record: the UI's per-change "undo" can't undo a revert (the snapshot is
        # popped), so don't offer the affordance.
        action = {"tool": "revert_file", "tool_kind": "write", "path": rel, "verb": f"revert {rel}",
                  "label": f"revert {rel}", "preview": f"restore {rel} from its pre-edit snapshot"}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        ok = self.backups.revert(p)
        if ok:
            self.applied.append(action)
            return f"(reverted {rel})"
        return f"(no snapshot to revert for {rel})"

    def revert(self, path: str) -> str:
        p, rel, err = self._check(path)
        if err:
            return err
        if not self.backups:
            return "(no snapshots available to revert)"
        ok = self.backups.revert(p)
        return f"(reverted {rel})" if ok else f"(no snapshot to revert for {rel})"

    def revert_exact(self, path: str, recovery_id: str, expected_postimage: dict,
                     expected_postimage_mode) -> Optional[dict]:
        """Ungated, receipt-bound operator Undo; ``None`` means conflict and never means success."""
        p, rel, err = self._check(path)
        if err or not self.backups:
            return None
        status = self.backups.revert_exact(
            p, recovery_id, expected_postimage, expected_postimage_mode)
        if status is None:
            return None
        return {"status": status, "message": f"(reverted {rel})"}
