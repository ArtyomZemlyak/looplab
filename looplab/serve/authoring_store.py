"""Files-as-truth AUTHORING: the bounded listing reads behind `GET /api/{kind}` and the durable
operation-receipt store behind `PUT /api/{kind}/{name}/operations/{operation_id}` — plus that
operation's read-only observation GET and the legacy last-writer-wins `PUT /api/{kind}/{name}`.

Review 2026-09-22, SRV2-13 (doc 50 SR-04). About 800 lines of `serve/routers/misc.py` — receipts,
an interprocess lock, a 4,096-receipt quota, a v1 schema — sat in the grab-bag router, so every
state of the receipt machine (a prepared receipt whose file write never landed, an exhausted quota,
an intervening write) was reachable only by building the ASGI app and driving HTTP. The bodies are
verbatim moves. The one new function is `author_inventory`: `list_author`'s file-reading half with
its comments, cut where every remaining line reads a listing bound.

ONE PROTOCOL, ONE MODULE. The listing MINTS the two CAS tokens a write must present — each row's
`revision` (`_author_revision`) and the root's `target_root_id` (`_author_target_root_id`) — and
the operation store CHECKS them, so the two halves live beside each other rather than on either
side of a module boundary.

What stays in the router, and why: the route declarations with their parameter validation and
their `Cache-Control` lines, `_require_writable_author_kind` (a 404/405 decision about the URL),
and `_authoring_http_failure`, the ONE translation of `_AuthoringFailure` into an HTTP refusal.
Nothing here imports FastAPI: this module raises `_AuthoringFailure` and returns plain dicts.

THE BOUNDS ARE THIS MODULE'S. Every read of `_AUTHOR_MAX_FILES`, `_AUTHOR_SKILL_*` and
`_AUTHOR_MAX_RECEIPTS` is here, and so is every call of `_read_author_file_safely`, so a test
narrows a bound or swaps the reader by patching THIS module. The router imports `_AUTHOR_MAX_BYTES`
by name for its two body-size refusals — the one-definition direction `routers/runs.py` takes from
`serve/concept_lens_service.py` — so patching it here moves the listing and the receipts but not
those two 400s.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any

from looplab.core.atomicio import (
    _ensure_strict_parent, atomic_write_text, file_identity, same_file_entry,
    strict_atomic_write_bytes, strict_atomic_write_text, strict_fsync_parent,
)
from looplab.core.pathsafe import is_reparse
from looplab.events.eventstore import EventStoreLockError, interprocess_lock


# Bounds for the files-as-truth authoring routes. `knowledge_dir` is agent-writable (the engine's own
# `remember` tool), so `GET /api/{kind}` needs a hard ceiling or the loop can grow its own OOM; the
# name guard keeps `PUT /api/{kind}/{name}` to the authored-markdown surface `list_author` can show.
_AUTHOR_MAX_FILES = 500
_AUTHOR_MAX_BYTES = 256 * 1024
# THE AUTHORING SURFACE'S ROOTS. `memory_skills` (2026-09-08) is the fourth and the only one the
# operator does not write: `<memory_dir>/skills`, the auto-distilled cards
# `engine/memory.py::write_auto_skill` drafts from a run's own supported work items. Until it was
# listed here those cards were invisible to the operator until CROSS-TASK promotion moved them into
# the production listing (`tools/skills.py::skill_tier` — an auto card is `task`, and `task` stays
# out of the run-time listing), so the one party who could judge a candidate could only read it by
# opening files on the host. That was doc 27's `auto-distilled-skills-outside-authoring`.
#
# READ-ONLY, and not as a convenience: every file there carries the lifecycle frontmatter
# (`status`, `claim_sha256`, `fingerprints`, `demotions`) that `write_auto_skill`'s
# read-modify-write and the production visibility gate both key on, so a hand edit through this
# surface is an unreviewed write to a trust boundary — one that could promote a one-task candidate
# by typing a word. The review question this root answers ("what did my runs distil, and is it
# true?") does not need a write; retiring a bad card is a file deletion on the host, which this
# surface has never offered for any kind.
_AUTHOR_KINDS = ("prompts", "skills", "knowledge", "memory_skills")
_AUTHOR_WRITABLE_KINDS = ("prompts", "skills", "knowledge")
# Recursive skill packages are a display surface, not permission to walk an operator-controlled
# tree without end. These bounds cap both directory work and relative-path complexity independently
# of the response/file caps above.
_AUTHOR_SKILL_SCAN_ENTRIES = 5000
_AUTHOR_SKILL_MAX_DEPTH = 16
_AUTHOR_SKILL_DISPLAY_MAX_BYTES = 4096
_AUTHOR_OPERATION_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_AUTHOR_REVISION_RE = re.compile(r"\A(?:missing|sha256:[0-9a-f]{64})\Z")
_AUTHOR_RESULT_REVISION_RE = re.compile(r"\A(?:missing|oversized|sha256:[0-9a-f]{64})\Z")
_AUTHOR_TARGET_ROOT_ID_RE = re.compile(r"\Aroot-sha256:[0-9a-f]{64}\Z")
_AUTHOR_OPERATION_SCHEMA = "looplab.authoring-operation/v1"
_AUTHOR_MISSING_REVISION = "missing"
_AUTHOR_OVERSIZED_REVISION = "oversized"
# Reuse the already-reserved root-sidecar namespace. Unlike `assistant/<sid>`, this directory has no
# user-addressable recursive-delete route, so an operation receipt cannot be erased through another API.
_AUTHOR_STATE_PARENT = ".command-locks"
_AUTHOR_RECEIPT_DIR = ".authoring-operations"
_AUTHOR_RECEIPT_LOCK = ".authoring-operations.lock"
_AUTHOR_MAX_RECEIPTS = 4096
_AUTHOR_RECEIPT_FIELDS = frozenset({
    "schema", "operation_id", "kind", "name", "target_root", "expected_revision",
    "target_root_id", "desired_revision", "status", "result_revision", "code",
    "created_at", "updated_at",
})
_AUTHOR_THREAD_LOCK = threading.Lock()


def _valid_author_name(value: object) -> bool:
    """Accept every safe, visible markdown basename, including spaces and Unicode."""
    return (isinstance(value, str)
            and 3 < len(value) <= 255
            and value.endswith(".md")
            and "/" not in value and "\\" not in value
            and Path(value).name == value
            and not any(unicodedata.category(ch).startswith("C") for ch in value))


def _valid_skill_display_name(value: object) -> bool:
    """A safe POSIX-style relative display id for a nested ``*/SKILL.md`` package.

    This is deliberately NOT a write name. ``_valid_author_name`` remains the only write/recovery
    identity and rejects every slash; the relative id exists only so Authoring can inspect the same
    packaged skills the runtime discovers.
    """
    if not isinstance(value, str) or "\\" in value:
        return False
    try:
        if len(value.encode("utf-8")) > _AUTHOR_SKILL_DISPLAY_MAX_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    parts = value.split("/")
    return (2 <= len(parts) <= _AUTHOR_SKILL_MAX_DEPTH + 1
            and parts[-1] == "SKILL.md"
            and all(part not in ("", ".", "..") and len(part) <= 255
                    and not any(unicodedata.category(ch).startswith("C") for ch in part)
                    for part in parts))


def _author_parent_chain_is_safe(root: Path, relative: Path) -> bool:
    """Re-check every nested parent without following a symlink/reparse point."""
    current = root
    try:
        root_entry = root.lstat()
        if is_reparse(root_entry) or not stat.S_ISDIR(root_entry.st_mode):
            return False
        for part in relative.parts[:-1]:
            current = current / part
            entry = current.lstat()
            if is_reparse(entry) or not stat.S_ISDIR(entry.st_mode):
                return False
    except (OSError, RuntimeError):
        return False
    return True


def _read_author_file_safely(root: Path, path: Path) -> bytes | None:
    """Read one bounded stable regular file and discard any link/path replacement race.

    NOT routed through `core/trace_files.py::open_private_trace_file`, and the reasons are specific
    rather than historical — this is the same read-then-verify dance (lstat -> `O_NOFOLLOW` open ->
    fstat identity -> re-lstat -> compare), so the resemblance is real and a reader should know why
    the two are separate:

      * it answers about a file under an operator's AUTHORING root, not a run-private sidecar, so it
        additionally proves the whole PARENT CHAIN is safe and that the resolved path is still inside
        `root` — questions the trace helper never has to ask, because its callers already hold a run
        directory;
      * it must not require `st_nlink == 1`. `assert_private_trace_file` does, correctly, for a file
        the service itself created; a skill file is an ordinary repo file an operator may legitimately
        have hard-linked, and refusing it would be a new refusal dressed as a refactor;
      * it returns `None` where the shared helper raises, because every caller here treats
        unreadable and absent alike.

    What the two MUST keep in common is the identity ladder itself. If a race is ever added to
    `_changed()` there, add it here; the mixed `same_file_entry`/`file_identity` tiers below are the
    same weak/strong pair that helper uses, spelled out because this function needs both at four
    comparison points rather than two.
    """
    try:
        relative = path.relative_to(root)
    except (ValueError, RuntimeError):
        return None
    if not _author_parent_chain_is_safe(root, relative):
        return None
    try:
        before = path.lstat()
    except OSError:
        return None
    if is_reparse(before) or not stat.S_ISREG(before.st_mode):
        return None
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (is_reparse(opened) or not stat.S_ISREG(opened.st_mode)
                or same_file_entry(opened) != same_file_entry(before)):
            return None
        chunks: list[bytes] = []
        remaining = _AUTHOR_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_read = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        after = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if (is_reparse(after) or not stat.S_ISREG(after.st_mode)
            # Compare like stat interfaces so Windows' lstat/fstat ctime representation cannot
            # reject every healthy file. The weak tier binds the descriptor to the path entry;
            # the full tier then proves both the path and the open descriptor stayed unchanged.
            or same_file_entry(opened) != same_file_entry(after)
            or file_identity(before) != file_identity(after)
            or file_identity(opened) != file_identity(after_read)
            or not _author_parent_chain_is_safe(root, relative)
            or (resolved != root and root not in resolved.parents)):
        return None
    return b"".join(chunks)


def _skill_author_candidates(root: Path) -> tuple[list[tuple[str, Path, bool]], int, bool]:
    """Return bounded safe root Markdown + nested package candidates.

    The integer is a lower bound on candidates already observed beyond the response cap. The boolean
    separately records that a directory/entry/depth cap prevented a complete inventory; it must not
    be represented as an invented omitted-file count because the unscanned directories may be empty.
    """
    candidates: list[tuple[str, Path, bool]] = []
    pending = deque([(root, (), 0)])
    scanned = 0
    incomplete = False
    max_candidates = _AUTHOR_MAX_FILES + 1
    while pending and len(candidates) < max_candidates:
        if scanned >= _AUTHOR_SKILL_SCAN_ENTRIES:
            incomplete = True
            break
        directory, relative_parts, depth = pending.popleft()
        try:
            directory_entry = directory.lstat()
            if is_reparse(directory_entry) or not stat.S_ISDIR(directory_entry.st_mode):
                continue
            entries = []
            with os.scandir(directory) as stream:
                for entry in stream:
                    if scanned >= _AUTHOR_SKILL_SCAN_ENTRIES:
                        incomplete = True
                        break
                    scanned += 1
                    entries.append(entry)
        except OSError:
            continue
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            if (name in ("", ".", "..") or "/" in name or "\\" in name or len(name) > 255
                    or any(unicodedata.category(ch).startswith("C") for ch in name)):
                continue
            try:
                observed = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if is_reparse(observed):
                continue
            candidate = directory / name
            if stat.S_ISREG(observed.st_mode):
                if not relative_parts and _valid_author_name(name):
                    candidates.append((name, candidate, False))
                elif relative_parts and name == "SKILL.md":
                    display_name = "/".join((*relative_parts, name))
                    if _valid_skill_display_name(display_name):
                        candidates.append((display_name, candidate, True))
            elif stat.S_ISDIR(observed.st_mode):
                if depth >= _AUTHOR_SKILL_MAX_DEPTH:
                    incomplete = True
                else:
                    pending.append((candidate, (*relative_parts, name), depth + 1))
            if len(candidates) >= max_candidates:
                incomplete = True
                break
    # Preserve the historical flat editor under the shared response cap: writable root files sort
    # before the additive read-only package inventory, then each class is deterministic by name.
    candidates.sort(key=lambda item: (item[2], item[0]))
    omitted = max(0, len(candidates) - _AUTHOR_MAX_FILES)
    return candidates[:_AUTHOR_MAX_FILES], omitted, incomplete


class _AuthoringFailure(RuntimeError):
    """Stable HTTP-facing failure for the authoring operation store."""

    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool):
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        super().__init__(message)



def _author_revision(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _author_target_root_id(root: Path) -> str:
    # `resolve()` is performed by the caller. normcase closes aliases on case-insensitive desktop
    # filesystems without exposing the canonical path itself on the wire.
    canonical = os.path.normcase(os.path.abspath(os.fspath(root)))
    material = f"looplab-authoring-root-v1\0{canonical}".encode("utf-8")
    return f"root-sha256:{hashlib.sha256(material).hexdigest()}"


def _author_state_parent(srv, *, create: bool) -> Path:
    parent = srv.root.resolve() / _AUTHOR_STATE_PARENT
    try:
        entry = parent.lstat()
    except FileNotFoundError:
        if not create:
            return parent
        try:
            _ensure_strict_parent(parent)
            entry = parent.lstat()
        except (OSError, TimeoutError, RuntimeError) as exc:
            raise _AuthoringFailure(
                503, "authoring_receipt_unavailable",
                "The authoring metadata namespace could not be created.", retryable=True) from exc
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring metadata namespace could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISDIR(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring metadata namespace is not a trusted directory.", retryable=False)
    if create:
        try:
            strict_fsync_parent(parent)
        except (OSError, TimeoutError, RuntimeError) as exc:
            raise _AuthoringFailure(
                503, "authoring_receipt_unavailable",
                "The authoring metadata namespace could not be durably confirmed.",
                retryable=True) from exc
    return parent


def _author_operation_path(srv, operation_id: str) -> Path:
    directory = _author_state_parent(srv, create=False) / _AUTHOR_RECEIPT_DIR
    try:
        entry = directory.lstat()
    except FileNotFoundError:
        return directory / f"{operation_id}.json"
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt store could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISDIR(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt store is not a trusted directory.", retryable=False)
    return directory / f"{operation_id}.json"


def _author_operation_lock_path(srv) -> Path:
    path = _author_state_parent(srv, create=True) / _AUTHOR_RECEIPT_LOCK
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_lock_unavailable",
            "The authoring operation lock could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISREG(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_lock_invalid",
            "The authoring operation lock is not a trusted regular file.", retryable=False)
    return path


def _valid_author_receipt(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _AUTHOR_RECEIPT_FIELDS:
        return False
    operation_id = value.get("operation_id")
    kind = value.get("kind")
    name = value.get("name")
    target_root = value.get("target_root")
    target_root_id = value.get("target_root_id")
    expected = value.get("expected_revision")
    desired = value.get("desired_revision")
    status_value = value.get("status")
    result = value.get("result_revision")
    code = value.get("code")
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    if (value.get("schema") != _AUTHOR_OPERATION_SCHEMA
            or not isinstance(operation_id, str)
            or _AUTHOR_OPERATION_RE.fullmatch(operation_id) is None
            or kind not in ("prompts", "skills", "knowledge")
            or not _valid_author_name(name)
            or not isinstance(target_root, str) or not target_root or len(target_root) > 4096
            or not Path(target_root).is_absolute()
            or not isinstance(target_root_id, str)
            or _AUTHOR_TARGET_ROOT_ID_RE.fullmatch(target_root_id) is None
            or target_root_id != _author_target_root_id(Path(target_root))
            or not isinstance(expected, str) or _AUTHOR_REVISION_RE.fullmatch(expected) is None
            or not isinstance(desired, str) or _AUTHOR_REVISION_RE.fullmatch(desired) is None
            or desired == _AUTHOR_MISSING_REVISION
            or status_value not in ("prepared", "succeeded", "conflict")
            or type(created_at) is not int or created_at < 0 or created_at > 9_000_000_000_000_000
            or type(updated_at) is not int or updated_at < created_at
            or updated_at > 9_000_000_000_000_000):
        return False
    if status_value == "prepared":
        return result is None and code is None
    if status_value == "succeeded":
        return result == desired and code is None
    return (isinstance(result, str)
            and _AUTHOR_RESULT_REVISION_RE.fullmatch(result) is not None
            and code in {"authoring_revision_conflict", "authoring_intervening_write"})


def _load_author_receipt(path: Path) -> dict[str, Any] | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISREG(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt path is not a trusted regular file.", retryable=False)
    try:
        with path.open("rb") as handle:
            raw = handle.read(8193)
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt could not be read.", retryable=True) from exc
    if len(raw) > 8192:
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt is larger than its protocol bound.", retryable=False)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt is malformed.", retryable=False) from exc
    if not _valid_author_receipt(value):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt does not satisfy the v1 protocol.", retryable=False)
    return value


def _save_author_receipt(path: Path, receipt: dict[str, Any]) -> None:
    if not _valid_author_receipt(receipt):
        raise AssertionError("refusing to persist an invalid authoring receipt")
    encoded = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        strict_atomic_write_text(path, encoded)
    except (OSError, TimeoutError, RuntimeError) as exc:
        # The strict helper can fail after the atomic replace but before confirming the parent
        # directory. Treat that outcome as indeterminate. Repeating this exact PUT republishes the
        # same prepared/terminal receipt before it trusts the record or mutates the authored file.
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt could not be durably published.", retryable=True) from exc


def _public_author_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    public = {key: receipt[key] for key in (
        "schema", "operation_id", "kind", "name", "target_root_id", "expected_revision",
        "desired_revision", "status", "result_revision", "code", "created_at", "updated_at",
    )}
    public["ok"] = receipt["status"] == "succeeded"
    public["replayable"] = receipt["status"] == "prepared"
    return public


def _author_target_revision(target: Path) -> str:
    try:
        entry = target.lstat()
    except FileNotFoundError:
        return _AUTHOR_MISSING_REVISION
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_target_unavailable",
            "The authored file could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISREG(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_target_untrusted",
            "The authored path is not a trusted regular file.", retryable=False)
    try:
        with target.open("rb") as handle:
            raw = handle.read(_AUTHOR_MAX_BYTES + 1)
    except FileNotFoundError:
        return _AUTHOR_MISSING_REVISION
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_target_unavailable",
            "The authored file could not be read.", retryable=True) from exc
    if len(raw) > _AUTHOR_MAX_BYTES:
        return _AUTHOR_OVERSIZED_REVISION
    return _author_revision(raw)


def _assert_author_receipt_capacity(srv) -> None:
    """Bound the idempotency set without forgetting an accepted operation UUID.

    TTL/LRU deletion would let an old timed-out request be applied again after its tombstone vanished.
    A hard cap is the bounded policy that preserves permanent replay safety.
    """
    directory = _author_state_parent(srv, create=False) / _AUTHOR_RECEIPT_DIR
    try:
        entry = directory.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt store could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISDIR(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt store is not a trusted directory.", retryable=False)
    try:
        with os.scandir(directory) as entries:
            for index, _entry in enumerate(entries, start=1):
                if index >= _AUTHOR_MAX_RECEIPTS:
                    raise _AuthoringFailure(
                        409, "authoring_receipt_quota_exhausted",
                        f"The authoring receipt limit ({_AUTHOR_MAX_RECEIPTS}) is exhausted; "
                        "new operation identities are disabled to preserve idempotency.",
                        retryable=False)
    except _AuthoringFailure:
        raise
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt store could not be enumerated.", retryable=True) from exc


def _resolved_author_root(directory: Path | None, kind: str) -> Path:
    if directory is None:
        raise _AuthoringFailure(
            400, "authoring_directory_unconfigured",
            f"No {kind} directory is configured.", retryable=False)
    try:
        _ensure_strict_parent(directory)
        root = directory.resolve(strict=True)
        entry = root.stat()
        strict_fsync_parent(root)
    except (OSError, TimeoutError, RuntimeError) as exc:
        raise _AuthoringFailure(
            503, "authoring_directory_unavailable",
            f"The configured {kind} directory is unavailable.", retryable=True) from exc
    if not stat.S_ISDIR(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_directory_invalid",
            f"The configured {kind} path is not a directory.", retryable=False)
    return root


def _configured_author_root(directory: Path | None, kind: str) -> Path:
    """Resolve configuration identity without creating directories (safe for receipt GETs)."""
    if directory is None:
        raise _AuthoringFailure(
            409, "authoring_operation_target_changed",
            f"The configured {kind} directory is no longer available for this operation.",
            retryable=False)
    try:
        return directory.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _AuthoringFailure(
            503, "authoring_directory_unavailable",
            f"The configured {kind} directory identity could not be inspected.",
            retryable=True) from exc


def memory_skills_dir(settings) -> Path | None:
    """`<memory_dir>/skills` — the auto-distilled skill store — or None when no memory dir is set.

    ONE derivation, read by the listing route and by every refusal beside it. Deliberately NOT a
    `Settings` field of its own: the directory is not independently configurable anywhere in the
    engine (`engine/lessons_distill.py` writes `Path(memory_dir) / "skills"` and
    `tools/skills.py`'s auto library reads that same path), so a field here would be a second
    spelling of one place — i.e. a way for the writer and the reviewer to disagree about which
    cards exist. It moves with `memory_dir`, which is what the operator actually configures.
    """
    configured = getattr(settings, "memory_dir", None)
    return Path(configured) / "skills" if configured else None


def _current_author_directory(srv, kind: str) -> Path | None:
    """Read one configured-directory snapshot (mutation callers hold the settings lock)."""
    settings = srv.global_settings()
    configured = {
        "prompts": settings.prompt_dir,
        "skills": settings.skills_dir,
        "knowledge": settings.knowledge_dir,
        "memory_skills": memory_skills_dir(settings),
    }.get(kind)
    return Path(configured) if configured else None


def _new_author_receipt(*, operation_id: str, kind: str, name: str, target_root: Path,
                        target_root_id: str, expected_revision: str, desired_revision: str,
                        status_value: str,
                        result_revision: str | None = None,
                        code: str | None = None) -> dict[str, Any]:
    now = time.time_ns() // 1_000_000
    return {
        "schema": _AUTHOR_OPERATION_SCHEMA,
        "operation_id": operation_id,
        "kind": kind,
        "name": name,
        "target_root": str(target_root),
        "target_root_id": target_root_id,
        "expected_revision": expected_revision,
        "desired_revision": desired_revision,
        "status": status_value,
        "result_revision": result_revision,
        "code": code,
        "created_at": now,
        "updated_at": now,
    }


def _same_author_operation(receipt: dict[str, Any], *, operation_id: str, kind: str,
                           name: str, target_root_id: str, expected_revision: str,
                           desired_revision: str) -> bool:
    return (receipt["operation_id"] == operation_id
            and receipt["kind"] == kind
            and receipt["name"] == name
            and receipt["target_root_id"] == target_root_id
            and receipt["expected_revision"] == expected_revision
            and receipt["desired_revision"] == desired_revision)


def _run_author_operation(srv, *, kind: str, name: str, operation_id: str, text_bytes: bytes,
                          expected_revision: str,
                          expected_target_root_id: str) -> dict[str, Any]:
    desired_revision = _author_revision(text_bytes)
    receipt_path = _author_operation_path(srv, operation_id)
    try:
        # Hold both locks for the full transition. The settings lock makes "current root" a real
        # precondition rather than a Path captured just before the authoring lock was acquired.
        with _AUTHOR_THREAD_LOCK, interprocess_lock(
                _author_operation_lock_path(srv), required=True), \
                srv.settings.ui_settings_transaction():
            directory = _current_author_directory(srv, kind)
            receipt = _load_author_receipt(receipt_path)
            if receipt is not None:
                if not _same_author_operation(
                        receipt, operation_id=operation_id, kind=kind, name=name,
                        target_root_id=expected_target_root_id,
                        expected_revision=expected_revision,
                        desired_revision=desired_revision):
                    raise _AuthoringFailure(
                        409, "authoring_operation_conflict",
                        "That operation id is already bound to a different authoring payload.",
                        retryable=False)
                configured_root = _configured_author_root(directory, kind)
                configured_root_id = _author_target_root_id(configured_root)
                if (receipt["target_root_id"] != configured_root_id
                        or expected_target_root_id != configured_root_id):
                    raise _AuthoringFailure(
                        409, "authoring_operation_target_changed",
                        "The configured authoring directory changed after this operation began.",
                        retryable=False)
                # Re-publish the exact terminal record before trusting it. This is harmless after a
                # confirmed write and closes strict_atomic_write's visible-but-unconfirmed failure gap.
                if receipt["status"] != "prepared":
                    _save_author_receipt(receipt_path, receipt)
                    return _public_author_receipt(receipt)

            if receipt is None:
                _assert_author_receipt_capacity(srv)
                listed_root = _configured_author_root(directory, kind)
                if _author_target_root_id(listed_root) != expected_target_root_id:
                    raise _AuthoringFailure(
                        409, "authoring_operation_target_changed",
                        "The configured authoring directory changed after its list snapshot.",
                        retryable=False)

            root = _resolved_author_root(directory, kind)
            current_target_root_id = _author_target_root_id(root)
            if current_target_root_id != expected_target_root_id:
                raise _AuthoringFailure(
                    409, "authoring_operation_target_changed",
                    "The configured authoring directory changed after its list snapshot.",
                    retryable=False)
            target = root / name
            if receipt is not None and receipt["target_root_id"] != current_target_root_id:
                raise _AuthoringFailure(
                    409, "authoring_operation_target_changed",
                    "The configured authoring directory changed while this operation was pending.",
                    retryable=False)

            if receipt is None:
                current_revision = _author_target_revision(target)
                if current_revision != expected_revision:
                    receipt = _new_author_receipt(
                        operation_id=operation_id, kind=kind, name=name, target_root=root,
                        target_root_id=current_target_root_id,
                        expected_revision=expected_revision, desired_revision=desired_revision,
                        status_value="conflict", result_revision=current_revision,
                        code="authoring_revision_conflict")
                    _save_author_receipt(receipt_path, receipt)
                    return _public_author_receipt(receipt)
                receipt = _new_author_receipt(
                    operation_id=operation_id, kind=kind, name=name, target_root=root,
                    target_root_id=current_target_root_id,
                    expected_revision=expected_revision, desired_revision=desired_revision,
                    status_value="prepared")
            # A prepared record may be the visible side of a failed strict publication. Confirm the
            # same immutable intent before inspecting/replaying its file effect.
            _save_author_receipt(receipt_path, receipt)

            current_revision = _author_target_revision(target)
            if current_revision not in {expected_revision, desired_revision}:
                receipt = {
                    **receipt,
                    "status": "conflict",
                    "result_revision": current_revision,
                    "code": "authoring_intervening_write",
                    "updated_at": max(receipt["updated_at"], time.time_ns() // 1_000_000),
                }
                _save_author_receipt(receipt_path, receipt)
                return _public_author_receipt(receipt)

            # This second observation narrows the only unavoidable gap left by writers that do not
            # participate in the authoring lock (manual editors and generic owner file tools). The
            # CAS guarantee is exact across UI/API writers; an uncoordinated filesystem writer can
            # still race the atomic replace, so the postcondition below also fails closed.
            precommit_revision = _author_target_revision(target)
            if precommit_revision not in {expected_revision, desired_revision}:
                receipt = {
                    **receipt,
                    "status": "conflict",
                    "result_revision": precommit_revision,
                    "code": "authoring_intervening_write",
                    "updated_at": max(receipt["updated_at"], time.time_ns() // 1_000_000),
                }
                _save_author_receipt(receipt_path, receipt)
                return _public_author_receipt(receipt)

            # Rewriting identical bytes is intentional after an ambiguous target publication: it
            # obtains a fresh durability receipt without changing the operation's semantic result.
            try:
                strict_atomic_write_bytes(target, text_bytes)
            except (OSError, TimeoutError, RuntimeError) as exc:
                raise _AuthoringFailure(
                    503, "authoring_target_unavailable",
                    "The authored file write could not be durably confirmed.", retryable=True) from exc
            if _author_target_revision(target) != desired_revision:
                raise _AuthoringFailure(
                    503, "authoring_target_unavailable",
                    "The authored file did not match the submitted payload after its write.",
                    retryable=True)
            receipt = {
                **receipt,
                "status": "succeeded",
                "result_revision": desired_revision,
                "code": None,
                "updated_at": max(receipt["updated_at"], time.time_ns() // 1_000_000),
            }
            _save_author_receipt(receipt_path, receipt)
            return _public_author_receipt(receipt)
    except EventStoreLockError as exc:
        raise _AuthoringFailure(
            503, "authoring_lock_unavailable",
            "Authoring mutation serialization is unavailable.", retryable=True) from exc


def _lookup_author_operation(srv, *, kind: str, name: str, operation_id: str,
                             expected_target_root_id: str, expected_revision: str,
                             desired_revision: str) -> dict[str, Any]:
    try:
        with srv.settings.ui_settings_transaction():
            directory = _current_author_directory(srv, kind)
            receipt = _load_author_receipt(_author_operation_path(srv, operation_id))
            if receipt is None or receipt["kind"] != kind or receipt["name"] != name:
                raise _AuthoringFailure(
                    404, "authoring_operation_not_found",
                    "No receipt exists for that exact authoring operation.", retryable=False)
            if not _same_author_operation(
                    receipt, operation_id=operation_id, kind=kind, name=name,
                    target_root_id=expected_target_root_id,
                    expected_revision=expected_revision, desired_revision=desired_revision):
                raise _AuthoringFailure(
                    409, "authoring_operation_conflict",
                    "That operation id is bound to a different authoring payload or root identity.",
                    retryable=False)
            configured_root = _configured_author_root(directory, kind)
            if receipt["target_root_id"] != _author_target_root_id(configured_root):
                raise _AuthoringFailure(
                    409, "authoring_operation_target_changed",
                    "The configured authoring directory changed after this operation began.",
                    retryable=False)
            return _public_author_receipt(receipt)
    except EventStoreLockError as exc:
        raise _AuthoringFailure(
            503, "authoring_lock_unavailable",
            "Authoring receipt validation is unavailable.", retryable=True) from exc


def _run_legacy_author_write(srv, *, kind: str, name: str, text: str) -> dict[str, Any]:
    try:
        with _AUTHOR_THREAD_LOCK, interprocess_lock(
                _author_operation_lock_path(srv), required=True), \
                srv.settings.ui_settings_transaction():
            directory = _current_author_directory(srv, kind)
            root = _resolved_author_root(directory, kind)
            target = root / name
            current = _author_target_revision(target)
            if current == _AUTHOR_OVERSIZED_REVISION:
                raise _AuthoringFailure(
                    409, "authoring_target_oversized",
                    "The existing file is larger than the editor's safe write bound.",
                    retryable=False)
            try:
                atomic_write_text(target, text)
            except OSError as exc:
                raise _AuthoringFailure(
                    503, "authoring_target_unavailable",
                    "The authored file could not be written.", retryable=True) from exc
            return {"ok": True, "name": name,
                    "revision": _author_target_revision(target), "legacy": True}
    except EventStoreLockError as exc:
        raise _AuthoringFailure(
            503, "authoring_lock_unavailable",
            "Authoring mutation serialization is unavailable.", retryable=True) from exc


def author_inventory(root: Path, kind: str) -> tuple[list[dict[str, Any]], int, bool]:
    """The file rows `GET /api/{kind}` lists under one resolved, existing authoring `root`.

    Returns `(files, truncated_files, inventory_incomplete)`, the three listing fields that depend
    on reading the tree; the route owns the refusals and the root identity around them.
    """
    # Bounded: `knowledge_dir` is AGENT-WRITABLE (KnowledgeWriteTools.remember), so an unbounded
    # "read every *.md whole into one response" is a self-inflicted OOM the engine itself can grow.
    # Cap the file count and each file's bytes, and disclose truncation instead of silently lying
    # about completeness. A symlinked entry is skipped for the same reason /log refuses one.
    files = []
    if kind == "skills":
        candidates, truncated_files, inventory_incomplete = _skill_author_candidates(root)
    else:
        names = sorted(root.glob("*.md"))
        # `memory_skills` is the engine's own store: every row is read-only (see `_AUTHOR_KINDS`),
        # and the flat `auto-<digest>.md` shape it writes is why the name check below is a rule
        # about the NAME rather than about the flag — a read-only row is either a nested package
        # id or a plain basename, and neither is a write name here.
        read_only_kind = kind not in _AUTHOR_WRITABLE_KINDS
        candidates = [(p.name, p, read_only_kind) for p in names[:_AUTHOR_MAX_FILES]]
        truncated_files = max(0, len(names) - len(candidates))
        inventory_incomplete = False
    for display_name, p, read_only in candidates:
        if not (_valid_author_name(display_name)
                or (read_only and _valid_skill_display_name(display_name))):
            truncated_files += 1
            continue
        # knowledge_dir is AGENT-WRITABLE (see above), so a file can be deleted or renamed
        # between the glob and this open. Skip the one that vanished rather than 500-ing the
        # whole listing over a race that is normal here.
        head = _read_author_file_safely(root, p)
        if head is None:
            truncated_files += 1
            continue
        text = head[:_AUTHOR_MAX_BYTES].decode("utf-8", errors="replace")
        truncated = len(head) > _AUTHOR_MAX_BYTES
        files.append({"name": display_name, "text": text, "truncated": truncated,
                      "read_only": read_only,
                      # A truncated prefix cannot safely authorize replacement of bytes the
                      # editor never displayed. Such rows intentionally have no writable CAS
                      # token; operation PUTs reject every invented token against "oversized".
                      "revision": None if truncated else _author_revision(head)})
    return files, truncated_files, inventory_incomplete
