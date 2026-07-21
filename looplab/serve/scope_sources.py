"""Bounded, race-aware source capture for cross-run reports.

The ordinary run readers optimize for a live UI and therefore tolerate a torn
tail.  A paid, persisted cross-run report needs a stronger boundary: it must
bind every projection to one bounded set of bytes and must never silently mix
two filesystem generations while doing so.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

import orjson

from looplab.core.models import Event
from looplab.events.eventstore import MAX_EVENT_BATCH_BYTES, decode_event_record
from looplab.serve.run_commands import run_generation_token

MAX_SCOPE_EVENT_BYTES = 32 * 1024 * 1024
MAX_SCOPE_TOTAL_EVENT_BYTES = 128 * 1024 * 1024
MAX_SCOPE_TASK_BYTES = 1 * 1024 * 1024
MAX_SCOPE_CONFIG_BYTES = 256 * 1024

_MISSING_DIGEST = hashlib.sha256(b"<missing>").hexdigest()
_READ_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_T = TypeVar("_T")


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _WindowsFileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        )

    _get_file_information_by_handle_ex = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).GetFileInformationByHandleEx
    _get_file_information_by_handle_ex.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _get_file_information_by_handle_ex.restype = wintypes.BOOL
else:
    _get_file_information_by_handle_ex = None


class ScopeSourceError(RuntimeError):
    """A run source cannot be captured as authoritative evidence."""


class ScopeSourceCapacityError(ScopeSourceError):
    """A source exceeds a cross-run report's explicit byte budget."""


class ScopeSourceCorruptError(ScopeSourceError):
    """A source is present but is not a valid LoopLab run snapshot."""


class ScopeSourceChangedError(ScopeSourceError):
    """A source changed identity or content while it was being captured."""


@dataclass(frozen=True, slots=True)
class FrozenScopeSource:
    """One immutable binding between parsed report inputs and their bytes."""

    run_dir: Path
    events: tuple[Event, ...]
    task_doc: dict | None
    config_doc: dict
    event_bytes: int
    revision: dict


@dataclass(frozen=True, slots=True)
class _CapturedFile:
    path: Path
    raw: bytes | None
    status: os.stat_result | None
    change_time: int | None


def _ns(status: os.stat_result, field: str) -> int:
    value = getattr(status, f"st_{field}_ns", None)
    if value is not None:
        return int(value)
    return int(getattr(status, f"st_{field}") * 1_000_000_000)


def _attributes(status: os.stat_result) -> int:
    return int(getattr(status, "st_file_attributes", 0))


def _is_reparse(status: os.stat_result) -> bool:
    return bool(_attributes(status) & _REPARSE_POINT)


def _file_identity(status: os.stat_result) -> tuple[int, ...]:
    """Identity shared by lstat/fstat without Windows' divergent ctime."""
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_mode),
        int(status.st_size),
        _ns(status, "mtime"),
        _attributes(status),
    )


def _file_observation(status: os.stat_result) -> tuple[int, ...]:
    """Same-reader observation; ctime catches same-size A/B/A rewrites."""
    # On Windows ``st_ctime`` is creation time. ``_descriptor_change_time`` supplies the distinct NTFS
    # ChangeTime token while the file is open; retaining ctime here still strengthens path observations
    # and preserves the existing portable identity contract.
    return (*_file_identity(status), _ns(status, "ctime"))


def _descriptor_change_time(descriptor: int) -> int | None:
    """Return Windows FILE_BASIC_INFO.ChangeTime for one already-open descriptor.

    CPython exposes creation time, not NTFS ChangeTime, as ``st_ctime`` on Windows. The native token is
    therefore required to detect a same-size A/B/A rewrite whose writer restores ``mtime``. Other
    platforms retain their existing ctime-based observation and return ``None`` here.
    """
    if _get_file_information_by_handle_ex is None:
        return None
    info = _WindowsFileBasicInfo()
    handle = msvcrt.get_osfhandle(descriptor)
    if not _get_file_information_by_handle_ex(
        wintypes.HANDLE(handle),
        0,  # FILE_INFO_BY_HANDLE_CLASS.FileBasicInfo
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "could not query FILE_BASIC_INFO.ChangeTime")
    return int(info.ChangeTime)


def _directory_identity(status: os.stat_result) -> tuple[int, ...]:
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_mode),
        _attributes(status),
    )


def _lstat(path: Path, *, changed: bool = False) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        error = ScopeSourceChangedError if changed else ScopeSourceError
        raise error(f"scope source is unavailable: {path.name}") from exc


def _require_real_directory(path: Path, *, label: str) -> os.stat_result:
    status = _lstat(path)
    if stat.S_ISLNK(status.st_mode) or _is_reparse(status):
        raise ScopeSourceCorruptError(f"{label} must not be a symlink or reparse point")
    if not stat.S_ISDIR(status.st_mode):
        raise ScopeSourceCorruptError(f"{label} is not a directory")
    return status


def _run_path(root: Path, run_id: str) -> tuple[Path, os.stat_result]:
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id in {".", ".."}
        or "\x00" in run_id
        or "/" in run_id
        or "\\" in run_id
        or ":" in run_id
        or run_id.rstrip(" .") != run_id
    ):
        raise ScopeSourceCorruptError("run id is not a lexical direct child")
    try:
        root = Path(root).absolute()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScopeSourceError("scope root is unavailable") from exc
    _require_real_directory(root, label="scope root")
    run_dir = root / run_id
    if run_dir.parent != root:
        raise ScopeSourceCorruptError("run id is not a lexical direct child")
    return run_dir, _require_real_directory(run_dir, label="run directory")


def _require_regular(status: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(status.st_mode) or _is_reparse(status):
        raise ScopeSourceCorruptError(f"{label} must not be a symlink or reparse point")
    if not stat.S_ISREG(status.st_mode):
        raise ScopeSourceCorruptError(f"{label} is not a regular file")


def _open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_failure(path: Path, before: os.stat_result, exc: OSError) -> ScopeSourceError:
    try:
        after = os.lstat(path)
    except OSError:
        return ScopeSourceChangedError(f"scope source changed while opening: {path.name}")
    if _file_observation(after) != _file_observation(before):
        return ScopeSourceChangedError(f"scope source changed while opening: {path.name}")
    return ScopeSourceError(f"scope source could not be opened: {path.name}")


def _read_from_stable_file(
    path: Path,
    before: os.stat_result,
    reader: Callable[[int, int], _T],
) -> tuple[_T, os.stat_result, int | None]:
    try:
        descriptor = os.open(path, _open_flags())
    except OSError as exc:
        raise _open_failure(path, before, exc) from exc

    result: _T | None = None
    problem: Exception | None = None
    opened_change_time: int | None = None
    opened_change_time_valid = False
    try:
        try:
            opened = os.fstat(descriptor)
            _require_regular(opened, label=path.name)
            if _file_identity(opened) != _file_identity(before):
                raise ScopeSourceChangedError(
                    f"scope source changed before reading: {path.name}"
                )
            opened_change_time = _descriptor_change_time(descriptor)
            opened_change_time_valid = True
            result = reader(descriptor, int(before.st_size))
        except Exception as exc:  # noqa: BLE001 - revalidate identity before propagating
            problem = exc
        try:
            after_read = os.fstat(descriptor)
            after_change_time = _descriptor_change_time(descriptor)
        except OSError as exc:
            raise ScopeSourceChangedError(
                f"scope source changed while reading: {path.name}"
            ) from exc
        if (_file_observation(after_read) != _file_observation(opened)
                or (opened_change_time_valid
                    and after_change_time != opened_change_time)):
            raise ScopeSourceChangedError(f"scope source changed while reading: {path.name}")
    finally:
        os.close(descriptor)

    try:
        after = os.lstat(path)
    except OSError as exc:
        raise ScopeSourceChangedError(
            f"scope source disappeared after reading: {path.name}"
        ) from exc
    if _file_observation(after) != _file_observation(before):
        raise ScopeSourceChangedError(f"scope source changed after reading: {path.name}")
    if problem is not None:
        if isinstance(problem, ScopeSourceError):
            raise problem
        if isinstance(problem, OSError):
            raise ScopeSourceError(f"scope source could not be read: {path.name}") from problem
        raise problem
    assert result is not None
    return result, after, after_change_time


def _read_exact(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != expected_size:
        raise ScopeSourceChangedError("scope source size changed during exact read")
    return raw


def _capture_file(path: Path, *, limit: int, required: bool) -> _CapturedFile:
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        if required:
            raise ScopeSourceCorruptError(f"required scope source is missing: {path.name}") from exc
        return _CapturedFile(path=path, raw=None, status=None, change_time=None)
    except OSError as exc:
        raise ScopeSourceError(f"scope source is unavailable: {path.name}") from exc
    _require_regular(before, label=path.name)
    if before.st_size > limit:
        raise ScopeSourceCapacityError(
            f"scope source exceeds its {limit}-byte limit: {path.name}"
        )
    raw, after, change_time = _read_from_stable_file(path, before, _read_exact)
    return _CapturedFile(path=path, raw=raw, status=after, change_time=change_time)


def _revalidate_file(captured: _CapturedFile) -> None:
    if captured.status is None:
        try:
            os.lstat(captured.path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ScopeSourceChangedError(
                f"optional scope source changed: {captured.path.name}"
            ) from exc
        raise ScopeSourceChangedError(
            f"optional scope source appeared during capture: {captured.path.name}"
        )
    try:
        current = os.lstat(captured.path)
    except OSError as exc:
        raise ScopeSourceChangedError(
            f"scope source disappeared during capture: {captured.path.name}"
        ) from exc
    if _file_observation(current) != _file_observation(captured.status):
        raise ScopeSourceChangedError(
            f"scope source changed during capture: {captured.path.name}"
        )
    if captured.change_time is not None:
        try:
            _, _, current_change_time = _read_from_stable_file(
                captured.path, current, lambda _descriptor, _size: True
            )
        except ScopeSourceChangedError:
            raise
        except ScopeSourceError as exc:
            raise ScopeSourceChangedError(
                f"scope source could not be revalidated: {captured.path.name}"
            ) from exc
        if current_change_time != captured.change_time:
            raise ScopeSourceChangedError(
                f"scope source changed during capture: {captured.path.name}"
            )


def _events_from_line(raw: bytes, *, line_number: int) -> tuple[Event, ...]:
    """Strictly expand one physical event-log row into its logical event sequence."""

    try:
        value = orjson.loads(raw)
    except (orjson.JSONDecodeError, RecursionError) as exc:
        raise ScopeSourceCorruptError(
            f"event log has invalid JSON at complete line {line_number}"
        ) from exc
    if not isinstance(value, dict):
        raise ScopeSourceCorruptError(
            f"event log line {line_number} is not a JSON object"
        )
    try:
        events = tuple(decode_event_record(value, strict=True))
    except Exception as exc:  # noqa: BLE001 - Pydantic exposes several validation failure types
        raise ScopeSourceCorruptError(
            f"event log has an invalid event at complete line {line_number}"
        ) from exc
    if not events:
        raise ScopeSourceCorruptError(f"event log line {line_number} has no logical events")
    for event in events:
        if event.v != 1:
            raise ScopeSourceCorruptError(f"event log line {line_number} has unsupported version")
        if type(event.seq) is not int:
            raise ScopeSourceCorruptError(f"event log line {line_number} has a non-integer seq")
        if not math.isfinite(event.ts):
            raise ScopeSourceCorruptError(
                f"event log line {line_number} has a non-finite timestamp"
            )
    return events


def _event_from_line(raw: bytes, *, line_number: int) -> Event:
    """Return the first logical Event for bounded first-record generation probes."""

    return _events_from_line(raw, line_number=line_number)[0]


def _parse_events(raw: bytes, *, run_id: str) -> tuple[Event, ...]:
    complete_lines = raw.split(b"\n")[:-1]
    events: list[Event] = []
    run_started_count = 0
    for line_number, line in enumerate(complete_lines, start=1):
        if not line.strip():
            raise ScopeSourceCorruptError(
                f"event log has a blank complete line at {line_number}"
            )
        for event in _events_from_line(line, line_number=line_number):
            if event.seq != len(events):
                raise ScopeSourceCorruptError(
                    f"event log sequence is not contiguous at complete line {line_number}"
                )
            if event.type == "run_started":
                run_started_count += 1
                if event.data.get("run_id") != run_id:
                    raise ScopeSourceCorruptError("run_started does not match the run directory")
            events.append(event)
    if not events:
        raise ScopeSourceCorruptError("event log has no complete events")
    if run_started_count == 0:
        raise ScopeSourceCorruptError("event log has no correlated run_started event")
    if run_started_count != 1:
        raise ScopeSourceCorruptError("event log has duplicate run_started events")
    return tuple(events)


def _json_object(raw: bytes, *, label: str) -> dict:
    try:
        value = orjson.loads(raw)
    except (orjson.JSONDecodeError, RecursionError) as exc:
        raise ScopeSourceCorruptError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ScopeSourceCorruptError(f"{label} is not a JSON object")
    return value


def _digest(captured: _CapturedFile) -> str:
    if captured.raw is None:
        return _MISSING_DIGEST
    return hashlib.sha256(captured.raw).hexdigest()


def _tail_digest(event: Event) -> str:
    raw = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _log_sig(run_id: str, generation: str, status: os.stat_result) -> list:
    return [
        run_id,
        generation,
        int(status.st_dev),
        int(status.st_ino),
        _ns(status, "ctime"),
        int(status.st_size),
        _ns(status, "mtime"),
    ]


def capture_scope_source(
    root: Path,
    run_id: str,
    *,
    event_budget_bytes: int = MAX_SCOPE_EVENT_BYTES,
) -> FrozenScopeSource:
    """Freeze bounded run evidence without following links or mixing revisions."""
    if type(event_budget_bytes) is not int or event_budget_bytes <= 0:
        raise ScopeSourceCapacityError("event byte budget must be a positive integer")
    event_limit = min(MAX_SCOPE_EVENT_BYTES, event_budget_bytes)
    run_dir, run_status = _run_path(root, run_id)

    # CODEX AGENT: this is the report-authority boundary.  Parse only bytes captured from one
    # lstat/open/fstat identity, then revalidate every pathname before publishing their digests.
    event_file = _capture_file(
        run_dir / "events.jsonl", limit=event_limit, required=True
    )
    task_file = _capture_file(
        run_dir / "task.snapshot.json", limit=MAX_SCOPE_TASK_BYTES, required=False
    )
    config_file = _capture_file(
        run_dir / "config.snapshot.json", limit=MAX_SCOPE_CONFIG_BYTES, required=False
    )
    assert event_file.raw is not None and event_file.status is not None
    events = _parse_events(event_file.raw, run_id=run_id)
    task_doc = (
        None
        if task_file.raw is None
        else _json_object(task_file.raw, label="task snapshot")
    )
    config_doc = (
        {}
        if config_file.raw is None
        else _json_object(config_file.raw, label="config snapshot")
    )

    for captured in (event_file, task_file, config_file):
        _revalidate_file(captured)
    try:
        current_run = os.lstat(run_dir)
    except OSError as exc:
        raise ScopeSourceChangedError("run directory disappeared during capture") from exc
    if _directory_identity(current_run) != _directory_identity(run_status):
        raise ScopeSourceChangedError("run directory changed during capture")

    generation = run_generation_token(events)
    if len(generation) != 64:
        raise ScopeSourceCorruptError("event log has no durable generation")
    revision = {
        "run_id": run_id,
        "generation": generation,
        "tail_seq": events[-1].seq,
        "event_count": len(events),
        "tail_digest": _tail_digest(events[-1]),
        "log_sig": _log_sig(run_id, generation, event_file.status),
        "events_digest": hashlib.sha256(event_file.raw).hexdigest(),
        "task_snapshot_digest": _digest(task_file),
        "config_snapshot_digest": _digest(config_file),
        "event_bytes": len(event_file.raw),
    }
    return FrozenScopeSource(
        run_dir=run_dir,
        events=events,
        task_doc=task_doc,
        config_doc=config_doc,
        event_bytes=len(event_file.raw),
        revision=revision,
    )


def scope_event_size(root: Path, run_id: str) -> int:
    """Return one bounded event-log size without parsing potentially corrupt bytes."""
    run_dir, _ = _run_path(root, run_id)
    path = run_dir / "events.jsonl"
    try:
        status = os.lstat(path)
    except FileNotFoundError as exc:
        raise ScopeSourceCorruptError("required scope source is missing: events.jsonl") from exc
    except OSError as exc:
        raise ScopeSourceError("scope event log is unavailable") from exc
    _require_regular(status, label=path.name)
    size = int(status.st_size)
    if size > MAX_SCOPE_EVENT_BYTES:
        raise ScopeSourceCapacityError("event log exceeds the per-run byte limit")
    return size


def _read_first_complete_line(descriptor: int, file_size: int, limit: int) -> bytes:
    readable = min(file_size, limit)
    chunks: list[bytes] = []
    consumed = 0
    while consumed < readable:
        chunk = os.read(descriptor, min(64 * 1024, readable - consumed))
        if not chunk:
            break
        consumed += len(chunk)
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            return b"".join(chunks)
        chunks.append(chunk)
    if file_size >= limit:
        raise ScopeSourceCapacityError("first event exceeds its bounded probe limit")
    raise ScopeSourceCorruptError("event log has no complete first event")


def probe_scope_log_sig(
    root: Path,
    run_id: str,
    *,
    first_line_limit: int = MAX_EVENT_BATCH_BYTES,
) -> list:
    """Return a reset-safe log signature after reading only its bounded first line."""
    if type(first_line_limit) is not int or first_line_limit <= 0:
        raise ScopeSourceCapacityError("first-line limit must be a positive integer")
    run_dir, run_status = _run_path(root, run_id)
    path = run_dir / "events.jsonl"
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        raise ScopeSourceCorruptError("required scope source is missing: events.jsonl") from exc
    except OSError as exc:
        raise ScopeSourceError("scope event log is unavailable") from exc
    _require_regular(before, label=path.name)
    if before.st_size > MAX_SCOPE_EVENT_BYTES:
        raise ScopeSourceCapacityError("event log exceeds the per-run byte limit")
    line_limit = min(first_line_limit, MAX_SCOPE_EVENT_BYTES)
    line, after, _change_time = _read_from_stable_file(
        path,
        before,
        lambda descriptor, size: _read_first_complete_line(
            descriptor, size, line_limit
        ),
    )
    if not line.strip():
        raise ScopeSourceCorruptError("event log has a blank first complete line")
    first = _event_from_line(line, line_number=1)
    if first.seq != 0:
        raise ScopeSourceCorruptError("event log does not start at sequence zero")
    if first.type == "run_started" and first.data.get("run_id") != run_id:
        raise ScopeSourceCorruptError("run_started does not match the run directory")
    generation = run_generation_token((first,))
    if len(generation) != 64:
        raise ScopeSourceCorruptError("event log has no durable generation")
    try:
        current_run = os.lstat(run_dir)
    except OSError as exc:
        raise ScopeSourceChangedError("run directory disappeared during probe") from exc
    if _directory_identity(current_run) != _directory_identity(run_status):
        raise ScopeSourceChangedError("run directory changed during probe")
    return _log_sig(run_id, generation, after)
