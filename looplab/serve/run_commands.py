"""Durable, idempotent lifecycle for run control commands.

The legacy ``/control`` endpoint appends an intent and leaves every caller to guess whether an
engine must be woken and whether the requested effect happened.  ``RunCommandService`` is the one
authoritative funnel for command-aware clients: it normalizes the same control payloads as the
legacy route, persists a per-run command record, appends at most one marked intent, applies the
command's engine policy, and records an observable postcondition.

Records deliberately contain only a SHA-256 digest of ``Idempotency-Key``.  One atomic JSON file per
command avoids a shared read/modify/write index and survives UI/server restarts.  The event carries
``_command_id`` so recovery can prove an intent was appended before retrying it.

The HTTP payload validation this funnel shares with the legacy route — ``normalize_control``, the
five per-event tables and the 35 rules they register — is ``serve/control_validation.py`` (doc 25
SC-01).  This module imports it and re-exports ONLY the names its own code calls; see that module's
docstring for why `_card_resource_envelope` is reached through the module object instead.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException
import orjson

from looplab.core.atomicio import atomic_write_text
from looplab.core.pathsafe import filesystem_identity
from looplab.core.models import Event
from looplab.core.run_deletion import RunDeletionStorageError, load_run_deletion_fence
from looplab.core.run_reset import RunResetStorageError, load_run_reset_marker
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.comment_projection import COMMENT_MAX_VERSION
from looplab.events.eventstore import (
    MAX_EVENT_BATCH_BYTES, EventStore, EventStoreConcurrencyError, EventStoreLockError,
    decode_event_record, event_sequence_continues, iter_event_jsonl)
from looplab.events.replay import fold
from looplab.events.types import (
    EV_APPROVAL_GRANTED, EV_CARD_RESOURCE_PINNED, EV_HINT, EV_HYPOTHESIS_UPDATED, EV_PAUSE,
    EV_RESTART, EV_RUN_ABORT, EV_SPEC_APPROVED, standing_hint_dedup_key)
from looplab.serve import control_validation
from looplab.serve.command_observation import CommandObservation, CommandObservationIndex
from looplab.serve.control_validation import (
    CONTROL_SPECS, EnginePolicy, _error, _normalize_finalize_data, normalize_control,
    task_file_for)
from looplab.serve.durable_op import refuse_unless_quiescent
from looplab.serve.engine_proc import (
    EngineSpawnOutcomeUnknown, _claim_and_spawn_resume, _engine_alive, _engine_liveness,
    _spawn_engine)
from looplab.serve.protocol import COLLABORATION_EVENTS, CONTROL_EVENTS


TERMINAL_STATUSES = frozenset({"succeeded", "noop", "failed", "rejected", "timed_out"})
_RETRY_GUARDED_EVENTS = frozenset(CONTROL_EVENTS)
_COMMAND_ID_RE = re.compile(r"^cmd_[0-9a-f]{32}$")
_RUN_GENERATION_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def run_generation_token(events) -> str:
    """Return the stable lowercase token for one non-empty event-log generation.

    An in-place reset archives the entire log and a replacement engine writes a new first event.
    Basing the token on that durable event keeps it stable as the same run grows, while making the
    old and replacement logs distinct without a mutable sidecar that could drift from events.jsonl.
    Empty/startup logs deliberately have no token: accepting a mutation before there is durable
    generation identity would re-open the exact reset race this precondition closes.
    """
    iterator = iter(events)
    try:
        try:
            first = next(iterator, None)
        except OSError:
            # A concurrent delete/replace or transient filesystem read failure is not a trustworthy
            # generation. Match EventStore.read_all's fail-closed empty-prefix behavior.
            return ""
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    if first is None:
        return ""
    if isinstance(first, dict):
        try:
            first = Event(**first)
        except Exception:  # noqa: BLE001 - match EventStore's fail-closed invalid-record boundary
            return ""
    seq = first.seq
    timestamp = first.ts
    event_type = first.type
    data = first.data or {}
    raw = json.dumps({
        "seq": seq, "ts": timestamp, "type": event_type,
        "run_id": data.get("run_id"),
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_expected_generation(value: object) -> str:
    """Normalize a wire generation token without coercing or trimming ambiguous input."""
    if not isinstance(value, str) or _RUN_GENERATION_RE.fullmatch(value) is None:
        raise HTTPException(400, {
            "code": "invalid_run_generation",
            "message": "expected_generation must be an exact 64-character hexadecimal string.",
            "remediation": "Refresh GET /state and submit its generation with this new command.",
        })
    return value.lower()


def _process_alive(pid: Optional[int]) -> Optional[bool]:
    """Return True/False only when process liveness is known; None means fail-closed unknown.

    Spawn leases use this after their observation deadline. A timeout is not evidence that a cold
    detached child died: clearing its lease could launch a second engine before the first imports
    enough code to expose ``engine.lock``. ``psutil`` gives the best zombie handling when installed;
    ``kill(pid, 0)`` is the dependency-free fallback. Permission/platform ambiguity stays unknown.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        import psutil  # optional proc extra
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
            return bool(proc.is_running())
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, psutil.Error):
            return None
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError as exc:
        # Windows reports a missing PID as ERROR_INVALID_PARAMETER (87); POSIX uses ESRCH.
        if exc.errno == errno.ESRCH or getattr(exc, "winerror", None) == 87:
            return False
        return None


def _lock_identity(path: Path) -> str:
    """Conservative per-run lock identity on common case-insensitive desktop filesystems."""
    # A case-sensitive macOS volume is safely over-serialized by the shared rule; it must never get
    # two locks for one default-volume run.
    return filesystem_identity(str(path.resolve()))


_PROCESS_IDENTITY_SCHEMES = frozenset({"proc-start", "psutil", "windows-filetime"})
_LEGACY_PROCESS_IDENTITY_SCHEME = "<legacy>"


def _process_identity_scheme(identity: object) -> Optional[str]:
    """Return a comparable source scheme, preserving pre-tag legacy identities."""
    if not isinstance(identity, str) or not identity:
        return None
    scheme, separator, token = identity.partition(":")
    if not separator:
        return _LEGACY_PROCESS_IDENTITY_SCHEME
    if scheme in _PROCESS_IDENTITY_SCHEMES and token:
        return scheme
    # Unknown/invalid tags may belong to a newer writer. Treat them as incomparable rather than
    # guessing that two different encodings prove PID reuse.
    return None


def _process_identity_proves_reuse(stored: object, current: object) -> bool:
    """Whether two non-equal identities conclusively describe different PID generations."""
    if not isinstance(stored, str) or not isinstance(current, str):
        return False
    if not stored or not current or stored == current:
        return False
    stored_scheme = _process_identity_scheme(stored)
    current_scheme = _process_identity_scheme(current)
    # Same-source tokens are comparable. This also retains the old behavior when both claims came
    # from pre-tag LoopLab versions. A tagged/legacy or cross-source mismatch is only ambiguity.
    return stored_scheme is not None and stored_scheme == current_scheme


def _process_identity(pid: Optional[int]) -> Optional[str]:
    """Source-tagged creation identity used to distinguish a live child from PID reuse."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        import psutil
        proc = psutil.Process(pid)
        created = proc.create_time()
        # Creation time is the stable PID-generation token. Do not mix cmdline into the hash:
        # transient AccessDenied could make the same live child compare as a recycled PID.
        raw = json.dumps({"pid": pid, "created": created}, sort_keys=True).encode("utf-8")
        return f"psutil:{hashlib.sha256(raw).hexdigest()}"
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - absent/exited/inaccessible process => identity unknown
        pass  # native Windows or /proc may still provide the same creation token
    if os.name == "nt":
        # ``looplab[ui]`` deliberately keeps psutil optional.  Windows has no ``/proc`` fallback,
        # but the process creation FILETIME is the same PID-generation token psutil exposes.  Read
        # it directly so a restarted UI can distinguish its dead worker from a recycled live PID
        # instead of quarantining the run forever merely because the optional ``proc`` extra is
        # absent. AccessDenied remains unknown/fail-closed and has the explicit recovery route below.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                        handle, ctypes.byref(created), ctypes.byref(exited),
                        ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                created_ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                raw = json.dumps(
                    {"pid": pid, "created_filetime": created_ticks}, sort_keys=True
                ).encode("utf-8")
                return f"windows-filetime:{hashlib.sha256(raw).hexdigest()}"
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    if os.name != "nt":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            start_ticks = stat.rsplit(")", 1)[1].split()[19]
            digest = hashlib.sha256(f"{pid}:{start_ticks}".encode("ascii")).hexdigest()
            return f"proc-start:{digest}"
        except (OSError, IndexError, UnicodeError, ValueError):
            pass
    return None


class RunCommandService:
    def __init__(self, srv, *, engine_alive: Callable[[Path], bool] = _engine_alive,
                 engine_liveness: Optional[Callable[[Path], Optional[bool]]] = None,
                 spawn_engine: Callable[..., Optional[int]] = _spawn_engine,
                 process_alive: Callable[[Optional[int]], Optional[bool]] = _process_alive,
                 process_identity: Callable[[Optional[int]], Optional[str]] = _process_identity,
                 startup_timeout: float = 3.0, command_timeout: float = 120.0,
                 poll_interval: float = 0.05,
                 max_observation_timeout: Optional[float] = None,
                 lock_acquire_timeout: float = 60.0):
        self.srv = srv
        self.engine_alive = engine_alive
        # Existing tests/integrations inject the historical bool probe. Treat those values as exact;
        # production uses the tri-state probe so unsupported/inaccessible ownership stays unknown.
        self.engine_liveness = (engine_liveness if engine_liveness is not None else
                                (_engine_liveness if engine_alive is _engine_alive else engine_alive))
        self.spawn_engine = spawn_engine
        self.process_alive = process_alive
        self.process_identity = process_identity
        self.startup_timeout = max(0.05, float(startup_timeout))
        self.command_timeout = max(self.startup_timeout, float(command_timeout))
        self.max_observation_timeout = max(
            self.command_timeout,
            float(max_observation_timeout) if max_observation_timeout is not None
            else max(300.0, self.command_timeout * 10),
        )
        self.poll_interval = max(0.01, float(poll_interval))
        self.lock_acquire_timeout = max(0.05, float(lock_acquire_timeout))
        self._local_lock = threading.RLock()
        self._run_locks: dict[str, threading.RLock] = {}
        self._command_observations = CommandObservationIndex(max_indexed_runs=8)

    def _engine_state(self, rd: Path) -> Optional[bool]:
        try:
            value = self.engine_liveness(rd)
        except OSError:
            return None
        if value is True:
            return True
        if value is False:
            return False
        return None

    @staticmethod
    def _engine_unknown_error(operation: str, *, retryable: bool = False) -> dict:
        return _error(
            "engine_liveness_unknown",
            f"cannot {operation} because engine ownership is unknown",
            ("inspect engine.lock and storage locking, then retry this command only after liveness "
             "is verifiable" if retryable else
             "inspect engine.lock and storage locking, then submit a new command with a new "
             "idempotency key after liveness is verifiable"),
            retryable=retryable,
        )

    def _lock_directory(self) -> Path:
        root = self.srv.root.resolve()
        directory = root / ".command-locks"
        try:
            if directory.is_symlink() or (directory.exists() and directory.resolve().parent != root):
                raise HTTPException(409, "run .command-locks must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or directory.resolve().parent != root:
                raise HTTPException(409, "run .command-locks changed during validation")
        except OSError as exc:
            raise HTTPException(409, f"run command-lock path cannot be validated: {exc}") from exc
        return directory

    def _sequence_path(self, rd: Path) -> Path:
        # Case-insensitive desktop filesystems must not give ``Foo`` and ``foo`` two OS locks/spawn
        # claims before either directory exists; `_lock_identity` normalizes Windows and default macOS.
        identity = _lock_identity(rd)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        path = self._lock_directory() / f"{digest}.lock"
        if path.is_symlink():
            raise HTTPException(409, "run command lock must not be a symlink")
        return path

    def _spawn_claim_path(self, rd: Path) -> Path:
        path = self._sequence_path(rd).with_suffix(".spawn.json")
        if path.is_symlink():
            raise HTTPException(409, "run spawn claim must not be a symlink")
        return path

    def _start_record_path(self, rd: Path) -> Path:
        """Root-sidecar path for the durable start operation occupying ``rd``.

        A start record must exist before the run directory does and must survive a partial
        materialization, so it cannot live underneath ``rd``. Deriving it from the sequencer path
        gives it exactly the same case/Unicode identity as the lock and spawn claim.
        """
        path = self._sequence_path(rd).with_suffix(".start.json")
        if path.is_symlink():
            raise HTTPException(409, "run start record must not be a symlink")
        return path

    def load_start_record(self, rd: Path) -> Optional[dict]:
        """Load one start sidecar, failing closed on unreadable or malformed ownership evidence."""
        path = self._start_record_path(rd)
        if not path.exists():
            return None
        record = self._load(path)
        # Atomic writers publish a complete JSON object. Once the path exists, an unreadable value
        # or a record without its exact operation identity is therefore unresolved evidence, never
        # permission for a caller to reserve the run or invoke Popen again.
        if record is None or not isinstance(record.get("id"), str) or not record.get("id"):
            raise HTTPException(503, {
                "code": "start_record_unavailable",
                "message": "The durable run-start record is unreadable or malformed.",
                "remediation": "Inspect the .command-locks start sidecar; do not start another engine.",
            })
        return record

    def save_start_record(self, rd: Path, record: dict) -> None:
        """Atomically publish a complete start record while the caller holds ``sequence(rd)``."""
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) \
                or not record.get("id"):
            raise ValueError("start record requires a non-empty string id")
        self._save(self._start_record_path(rd), record)

    def retire_start_record(self, rd: Path, start_id: str) -> bool:
        """Retire only the sidecar whose stored id exactly matches ``start_id``.

        The caller must hold ``sequence(rd)`` when retirement is part of delete/replacement. A
        mismatched id and malformed evidence are deliberately not cleared.
        """
        record = self.load_start_record(rd)
        if record is None or not isinstance(start_id, str) or record.get("id") != start_id:
            return False
        path = self._start_record_path(rd)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise HTTPException(503, f"could not retire run start record: {exc}") from exc
        return True

    def run_generation(self, rd: Path) -> str:
        """Stable identity of the event-log generation currently occupying a run id.

        Generation identity depends only on the first durable event. Read just that record so callers
        can validate the identity while holding the run sequencer without turning every poll into an
        O(events) critical section. ``iter_event_jsonl`` preserves the event store's torn/corrupt-first-line
        semantics; ``run_generation_token`` also validates that dictionary through ``Event`` so a
        complete JSON object with an invalid event schema remains generation-less, as in ``read_all``.
        """
        return run_generation_token(iter_event_jsonl(self._events_path(rd)))

    def run_generation_if_present(self, rd: Path) -> str:
        """Observe a generation while a fenced reset may temporarily have no event log.

        Ordinary command paths require ``events.jsonl`` and continue to use ``run_generation``.
        Replay removes that file before its matching child writes the replacement's first event, so
        its completion poll needs a missing-safe read without relaxing direct-child or reparse checks.
        """
        root = self.srv.root.resolve()
        requested = Path(rd)
        try:
            run_info = requested.lstat()
            canonical = requested.resolve()
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(404, "no such run") from exc
        run_attributes = int(getattr(run_info, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if (canonical == root or canonical.parent != root or not stat.S_ISDIR(run_info.st_mode)
                or requested.is_symlink()
                or bool(run_attributes & reparse_flag)):
            raise HTTPException(404, "no such run")

        events = canonical / "events.jsonl"
        try:
            info = events.lstat()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise HTTPException(409, "run event path cannot be validated") from exc
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        try:
            invalid = (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                       or bool(attributes & reparse_flag) or events.resolve().parent != canonical)
        except OSError as exc:
            raise HTTPException(409, "run event path cannot be validated") from exc
        if invalid:
            raise HTTPException(409, "run events.jsonl must be a regular in-run file")
        try:
            with open(events, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if ((info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
                        or not stat.S_ISREG(opened.st_mode)):
                    raise HTTPException(503, "run event identity changed during observation")
                raw = stream.readline(MAX_EVENT_BATCH_BYTES + 1)
        except FileNotFoundError:
            # A reset child has not written the first event yet, or an exact reset observation raced
            # the no-replace archive move. Both mean "not visible yet", never "no such run".
            return ""
        except OSError as exc:
            raise HTTPException(503, "run generation cannot be observed safely") from exc
        if not raw:
            return ""
        if len(raw) > MAX_EVENT_BATCH_BYTES:
            raise HTTPException(503, "replacement event prelude exceeds its safety limit")
        if not raw.endswith(b"\n"):
            # A torn first append is not a durable EventStore record. The exact child must also be
            # proven dead before this empty observation can ever authorize the same Popen again.
            return ""
        try:
            value = orjson.loads(raw.strip())
            if not isinstance(value, dict):
                raise ValueError("first event record is not an object")
            decoded = decode_event_record(value)
            if not event_sequence_continues(decoded, 0):
                raise ValueError("replacement event sequence does not begin at zero")
        except Exception as exc:  # noqa: BLE001 - complete malformed evidence must fail closed
            raise HTTPException(503, "replacement generation evidence is malformed") from exc
        return run_generation_token(decoded)

    @contextmanager
    def run_activity(self, rd: Path, kind: str, *, generation: str):
        """Lease a run generation for server-side work that can append while reset is possible."""
        token = secrets.token_hex(16)
        path = self._directory(rd) / f".activity_{token}.json"
        with self.sequence(rd):
            self._reject_unresolved_reset(rd, f"start {kind} activity")
            if self.run_generation(rd) != generation:
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "message": "The run was reset or replaced before this background work started.",
                    "remediation": "Refresh the run and submit the request against its current generation.",
                })
            now = time.time()
            owner = {"kind": str(kind)[:80], "pid": os.getpid(), "created_at": now}
            try:
                identity = self.process_identity(os.getpid())
            except Exception:  # noqa: BLE001
                identity = None
            if identity:
                owner["process_identity"] = identity
            self._save(path, owner)
        try:
            yield
        finally:
            try:
                with self.sequence(rd):
                    path.unlink(missing_ok=True)
            except (HTTPException, OSError):
                pass

    # ---- owner liveness: ONE decision, two claim carriers (doc 25 SC-12) ---------------------
    # A spawn claim arrives as an already-parsed dict; an execution claim arrives as a file that has
    # to be read (and may be a legacy bare-PID line). That is the only difference, and it belongs in
    # the loading, not in the decision — the decision below is a safety rule about process identity
    # that must answer the same way for both, because both gate the same thing: whether a second
    # worker may take over durable command state.

    def _owner_definitely_gone(self, row: dict) -> bool:
        """True only when a claim's owner CANNOT still be running.

        Deliberately asymmetric with `_owner_exactly_alive`: this is the destructive direction, so
        everything ambiguous — an unreadable pid, an inaccessible process, an identity token that
        cannot be compared — answers False and the claim is left alone.
        """
        pid = row.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            pid_state = self.process_alive(pid)
        except Exception:  # noqa: BLE001 - ambiguous ownership must fail closed
            pid_state = None
        if pid_state is False:
            return True
        stored_identity = row.get("process_identity")
        if isinstance(stored_identity, str) and stored_identity:
            try:
                current_identity = self.process_identity(pid)
            except Exception:  # noqa: BLE001 - ambiguous ownership must fail closed
                current_identity = None
            # A mismatch proves reuse only when both tokens share a comparable source encoding.
            if _process_identity_proves_reuse(stored_identity, current_identity):
                return True
        return False

    def _owner_exactly_alive(self, row: dict, *, own_process_counts: bool = False) -> bool:
        """True only when the claim names the exact live process generation that created it.

        `own_process_counts` is the ONE rule that differs between the two carriers, so it is a named
        argument rather than a second copy of the function. An EXECUTION claim written by this very
        server process counts as live even where creation identity is unavailable — its worker or
        activity context may still be running, and letting an operator clear it would be the same
        double-writer hazard the identity check exists to prevent. A SPAWN claim gets no such
        fallback: without a comparable identity it is simply not proof of an exact live generation.
        """
        pid = row.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            if self.process_alive(pid) is not True:
                return False
        except Exception:  # noqa: BLE001 - an inaccessible process is ambiguous, not known-live
            return False
        stored_identity = row.get("process_identity")
        if isinstance(stored_identity, str) and stored_identity:
            try:
                return self.process_identity(pid) == stored_identity
            except Exception:  # noqa: BLE001 - identity lookup failure stays fail-closed/uncertain
                return False
        return own_process_counts and pid == os.getpid()

    def _claim_child_definitely_gone(self, row: dict) -> bool:
        return self._owner_definitely_gone(row)

    def _claim_child_exactly_alive(self, row: dict) -> bool:
        """Whether a spawn claim names the exact currently-live PID generation."""
        return self._owner_exactly_alive(row)

    def _execution_owner_definitely_gone(self, path: Path) -> bool:
        """Return true only when a stale execution claim cannot still have a live owner.

        Age alone is not ownership evidence: a suspended worker can miss every heartbeat and later
        resume.  Reclaiming its file in that state would permit two workers to write terminal command
        state.  New claims therefore carry the server process creation identity; legacy bare-PID
        claims remain readable, and malformed/ambiguous claims fail closed.
        """
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return False
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return self._owner_definitely_gone(parsed)
        # Legacy bare-PID claim: adapt it to the same row shape rather than deciding again here.
        try:
            return self._owner_definitely_gone({"pid": int(raw)})
        except (TypeError, ValueError, OverflowError):
            return False

    def _execution_owner_exactly_alive(self, path: Path) -> bool:
        """True only when the claim names the exact live process generation that created it."""
        row = self._load(path)
        # `own_process_counts`: even where creation identity is unavailable, never let an operator
        # clear a claim owned by THIS server process — its worker/activity context may still run.
        return bool(row) and self._owner_exactly_alive(row, own_process_counts=True)

    def resolve_active_claims(self, rd: Path, confirmation: str = "") -> dict:
        """Retire orphaned execution/activity claims without ever clearing a proven live owner.

        Atomic hard-link publication below removes the normal empty-file crash window.  This route
        remains the guarded escape hatch for pre-upgrade, malformed, inaccessible-owner, or
        hard-link-fallback claims.  It is intentionally explicit because ambiguity is not evidence
        that a suspended worker died.
        """
        phrase = "I verified no LoopLab command or run activity is active"
        root = self.srv.root.resolve()
        canonical = rd.resolve()
        if canonical == root or canonical.parent != root or rd.is_symlink():
            raise HTTPException(400, "active-claim run must be a canonical direct child")
        with self.sequence(canonical):
            directory = self._directory(canonical)
            claims = [
                *directory.glob(".cmd_*.executing"),
                *directory.glob(".activity_*.json"),
            ] if directory.exists() else []
            if not claims:
                return {"ok": True, "resolved": False, "count": 0,
                        "reason": "no_active_claims"}

            unresolved: list[Path] = []
            retired = 0
            for claim in claims:
                if claim.is_symlink():
                    raise HTTPException(409, {
                        "code": "active_claim_symlink",
                        "message": "An active-claim path is a symbolic link and cannot be inspected safely.",
                        "remediation": "Inspect and remove the link locally; the API will not follow it.",
                    })
                try:
                    if self._execution_owner_definitely_gone(claim):
                        claim.unlink()
                        retired += 1
                        continue
                except OSError:
                    pass
                if self._execution_owner_exactly_alive(claim):
                    raise HTTPException(409, {
                        "code": "active_claim_owner_alive",
                        "message": "The exact process generation owning a command/activity claim is alive.",
                        "remediation": "Wait for it to finish or stop that process; never clear its live claim.",
                    })
                unresolved.append(claim)

            if not unresolved:
                return {"ok": True, "resolved": bool(retired), "count": retired,
                        "reason": "owners_definitively_gone"}
            now = time.time()
            minimum_age = max(5.0, self.startup_timeout * 2 + 1)
            for claim in unresolved:
                try:
                    created_at = float((self._load(claim) or {}).get("created_at")
                                       or claim.stat().st_mtime)
                except (OSError, TypeError, ValueError, OverflowError):
                    created_at = now
                if now - created_at < minimum_age:
                    raise HTTPException(409, {
                        "code": "active_claim_uncertain",
                        "message": "An unknown command/activity claim is still inside its safety window.",
                        "remediation": "Wait, inspect the process table, then retry explicit resolution.",
                    })
            if confirmation != phrase:
                raise HTTPException(409, {
                    "code": "active_claim_confirmation_required",
                    "message": "Claim ownership is unknown; automatic death proof is impossible.",
                    "remediation": f"After inspection, repeat with confirmation exactly: {phrase}",
                })

            # Revalidate immediately before unlinking. If any exact owner appeared/becomes provable,
            # leave every remaining claim intact rather than partially overriding live ownership.
            if any(self._execution_owner_exactly_alive(claim) for claim in unresolved):
                raise HTTPException(409, "an active claim owner became live during resolution")
            for claim in unresolved:
                try:
                    claim.unlink()
                    retired += 1
                except OSError as exc:
                    raise HTTPException(503, f"could not resolve active claim: {exc}") from exc
            return {"ok": True, "resolved": True, "count": retired,
                    "reason": "operator_verified_unknown_claims"}

    def _recent_spawn_claim(self, rd: Path) -> bool:
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        if path.exists() and (row is None or not isinstance(row.get("command_id"), str)
                              or not row.get("command_id")):
            return True  # malformed ownership evidence is unresolved, never permission to Popen
        if not row:
            return False
        # engine.lock is the startup postcondition for the lease itself. Once observed, the Popen
        # race is over and even an external/reset claim can be retired immediately.
        liveness = self._engine_state(rd)
        if liveness is True:
            try:
                path.unlink()
            except OSError:
                pass
            # Block THIS decision even though the lease can now be retired. A caller may have probed
            # liveness as false just before this probe observed the child acquire engine.lock; false
            # here would be interpreted as permission to Popen a duplicate (TOCTOU).
            return True
        if liveness is None:
            return True  # unknown ownership is an expiry-free anti-Popen fence
        quarantined = bool(row.get("quarantined"))
        if quarantined:
            if not self._claim_child_definitely_gone(row):
                return True
            # Definitive child death makes a retry/new driver safe again.
            try:
                path.unlink()
            except OSError:
                pass
            return False
        try:
            expires_at = float(row.get("expires_at"))
        except (TypeError, ValueError, OverflowError):
            try:
                expires_at = float(row.get("created_at")) + max(
                    self.command_timeout, self.startup_timeout * 2 + 1)
            except (TypeError, ValueError, OverflowError):
                expires_at = 0
        # A recorded child that has already exited is conclusive even inside the cold-start lease.
        # Waiting for the full observation timeout would make a pre-lock startup crash invisible and
        # block an otherwise safe retry for up to twenty minutes.
        if self._claim_child_definitely_gone(row):
            try:
                path.unlink()
            except OSError:
                pass
            return False
        if time.time() <= expires_at:
            return True
        # The observation deadline expiring is NOT proof that a detached child died. Promote the
        # lease to an expiry-free quarantine and release it only after engine.lock appears (handled
        # above) or the recorded PID is definitively gone. A missing PID is the crash window between
        # Popen and persisting its result and therefore remains unknown/fail-closed.
        row["quarantined"] = True
        row["quarantined_at"] = time.time()
        row["expires_at"] = None
        self._save(path, row)
        if not self._claim_child_definitely_gone(row):
            return True
        try:
            path.unlink()
        except OSError:
            pass
        return False

    def _record_spawn_claim(self, rd: Path, command_id: str, pid: Optional[int]) -> None:
        now = time.time()
        row = {"command_id": command_id, "created_at": now,
               "expires_at": now + self.max_observation_timeout,
               "pid": pid}
        if pid is not None:
            try:
                identity = self.process_identity(pid)
            except Exception:  # noqa: BLE001
                identity = None
            if identity:
                row["process_identity"] = identity
        self._save(self._spawn_claim_path(rd), row)

    def _quarantine_spawn_claim(self, rd: Path, command_id: str,
                                pid: Optional[int]) -> bool:
        """Keep an uncertain Popen owner until lock evidence or definitive PID death.

        Returns whether the claim is still unsafe after refreshing its process liveness.
        """
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        if path.exists() and (row is None or not isinstance(row.get("command_id"), str)
                              or not row.get("command_id")):
            return True
        if row and str(row.get("command_id") or "") != command_id:
            return self._recent_spawn_claim(rd)
        now = time.time()
        row = dict(row or {"command_id": command_id, "created_at": now})
        if pid is not None:
            row["pid"] = pid
            if not row.get("process_identity"):
                try:
                    identity = self.process_identity(pid)
                except Exception:  # noqa: BLE001
                    identity = None
                if identity:
                    row["process_identity"] = identity
        row["quarantined"] = True
        row["quarantined_at"] = now
        row["expires_at"] = None
        self._save(path, row)
        return self._recent_spawn_claim(rd)

    def _clear_spawn_claim(self, rd: Path, command_id: str) -> None:
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        if path.exists() and (row is None or not isinstance(row.get("command_id"), str)
                              or not row.get("command_id")):
            return
        if row and str(row.get("command_id") or "") != command_id:
            return
        try:
            path.unlink()
        except OSError:
            pass

    def spawn_inflight(self, rd: Path) -> bool:
        """True while a Popen is unresolved/quarantined, or on the decision that observes its lock."""
        return self._recent_spawn_claim(rd)

    def record_external_spawn(self, rd: Path, owner: str, pid: Optional[int]) -> None:
        """Register a legacy/reset Popen performed while the caller holds ``sequence(rd)``."""
        self._record_spawn_claim(rd, f"external:{owner}", pid)

    def begin_external_spawn(self, rd: Path, owner: str) -> None:
        """Install the crash-safe lease immediately before a legacy/reset Popen."""
        self._record_spawn_claim(rd, f"external:{owner}", None)

    def cancel_external_spawn(self, rd: Path, owner: str) -> None:
        self._clear_spawn_claim(rd, f"external:{owner}")

    def cancel_external_preclaim(self, rd: Path, owner: str) -> bool:
        """Retire only an exact PID-less lease known by the caller to precede Popen.

        The reset caller may use this only while holding ``sequence(rd)`` and while its durable
        receipt is still ``phase=archived``: reset saves ``popen_pending`` before invoking Popen, so
        that phase proves this exact PID-less row is the begin_external_spawn crash window. PID-less
        rows in every later phase remain uncertain and must never pass through this escape hatch.
        """
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        allowed = {
            "command_id", "created_at", "expires_at", "pid",
            "quarantined", "quarantined_at",
        }
        if (not path.exists() or not isinstance(row, dict)
                or set(row) - allowed
                or row.get("command_id") != f"external:{owner}"
                or "pid" not in row or row.get("pid") is not None
                or row.get("process_identity") is not None):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def observe_external_spawn(self, rd: Path, owner: str) -> str:
        """Observe the spawn claim correlated to ``owner`` without ever starting a process.

        The bounded result vocabulary is intentionally evidence-oriented:

        * ``absent``: no claim and no engine lock are visible;
        * ``live``: the engine lock is visible, including after its claim was already retired;
        * ``pending_known``: a matching claim names the exact live PID generation, pre-lock;
        * ``uncertain``: matching/malformed evidence cannot prove either liveness or death;
        * ``dead_or_cleared``: the matching claim was definitively dead and was retired;
        * ``mismatched``: the extant claim belongs to another external owner.

        This delegates expiry, quarantine, engine-lock retirement, PID-death and PID-reuse handling
        to ``_recent_spawn_claim``. It never exposes the stored PID creation identity. Callers that
        make a subsequent ownership decision should hold ``sequence(rd)`` across both operations.
        """
        expected = f"external:{owner}"
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        if not path.exists():
            liveness = self._engine_state(rd)
            return "live" if liveness is True else "absent" if liveness is False else "uncertain"
        if row is None or not isinstance(row.get("command_id"), str) \
                or not row.get("command_id"):
            # Preserve the existing fail-closed semantics (and any future quarantine bookkeeping).
            self._recent_spawn_claim(rd)
            return "uncertain"
        if row.get("command_id") != expected:
            # Still let the canonical observer retire a definitively dead/lock-observed claim, but
            # never alias another owner's operation to the supplied owner in this decision.
            self._recent_spawn_claim(rd)
            return "mismatched"

        active = self._recent_spawn_claim(rd)
        if not active:
            return "dead_or_cleared"

        # For a valid matching row, `_recent_spawn_claim` can return true with no remaining path only
        # on the decision that observed engine.lock and retired the lease. Preserve that positive
        # observation even if the lock changes immediately after the probe.
        if not path.exists():
            return "live"
        current = self._load(path)
        if current is None or not isinstance(current.get("command_id"), str) \
                or not current.get("command_id"):
            return "uncertain"
        if current.get("command_id") != expected:
            return "mismatched"
        liveness = self._engine_state(rd)
        if liveness is True:
            # The first `_recent_spawn_claim` probe may have raced just before lock acquisition. Run
            # it once more so the existing claim-retirement behavior remains centralized.
            self._recent_spawn_claim(rd)
            return "live"
        if liveness is None:
            return "uncertain"
        if self._claim_child_exactly_alive(current):
            return "pending_known"
        return "uncertain"

    def resolve_spawn_claim(self, rd: Path, confirmation: str = "") -> dict:
        """Safely retire a quarantined/unknown claim, with an explicit operator escape hatch.

        Known live children are never force-cleared. For an unreadable/identity-unknown claim the
        operator must provide an exact confirmation after independently checking the process table.
        """
        phrase = "I verified no LoopLab engine process is running"
        root = self.srv.root.resolve()
        canonical = rd.resolve()
        if canonical == root or canonical.parent != root or rd.is_symlink():
            raise HTTPException(400, "spawn-claim run must be a canonical direct child")
        with self.sequence(canonical):
            path = self._spawn_claim_path(canonical)
            if not path.exists():
                return {"ok": True, "resolved": False, "reason": "no_spawn_claim"}
            liveness = self._engine_state(canonical)
            if liveness is True:
                try:
                    path.unlink()
                except OSError as exc:
                    raise HTTPException(503, f"could not retire observed-live spawn claim: {exc}") from exc
                return {"ok": True, "resolved": True, "reason": "engine_lock_observed"}
            if liveness is None:
                raise HTTPException(409, self._engine_unknown_error("resolve the engine spawn claim"))

            row = self._load(path)
            if row and isinstance(row.get("command_id"), str):
                if self._claim_child_definitely_gone(row):
                    try:
                        path.unlink()
                    except OSError as exc:
                        raise HTTPException(503, f"could not retire dead-child spawn claim: {exc}") from exc
                    return {"ok": True, "resolved": True, "reason": "child_definitively_gone"}
                if self._claim_child_exactly_alive(row):
                    raise HTTPException(409, {
                        "code": "engine_start_uncertain",
                        "message": "The exact claimed child process is still alive.",
                        "remediation": "Inspect the process; never clear a live LoopLab child claim.",
                    })

            try:
                created_at = float((row or {}).get("quarantined_at")
                                   or (row or {}).get("created_at") or path.stat().st_mtime)
            except (OSError, TypeError, ValueError, OverflowError):
                created_at = time.time()
            minimum_age = max(5.0, self.startup_timeout * 2 + 1)
            if time.time() - created_at < minimum_age:
                raise HTTPException(409, {
                    "code": "engine_start_uncertain",
                    "message": "The unknown spawn claim is still inside its cold-start safety window.",
                    "remediation": "Wait, inspect the process table, then retry explicit resolution.",
                })
            if confirmation != phrase:
                raise HTTPException(409, {
                    "code": "spawn_claim_confirmation_required",
                    "message": "Process identity is unavailable; automatic child-death proof is impossible.",
                    "remediation": f"After inspection, repeat with confirmation exactly: {phrase}",
                })
            liveness = self._engine_state(canonical)  # final check before destructive unlink
            if liveness is not False:
                if liveness is None:
                    raise HTTPException(
                        409, self._engine_unknown_error("resolve the engine spawn claim"))
                raise HTTPException(409, "engine became live while resolving its spawn claim")
            try:
                path.unlink()
            except OSError as exc:
                raise HTTPException(503, f"could not resolve spawn claim: {exc}") from exc
            return {"ok": True, "resolved": True, "reason": "operator_verified_unknown_claim"}

    @contextmanager
    def sequence(self, rd: Path, *, timeout: Optional[float] = None):
        """Serialize one run's decision→intent→spawn boundary across threads/processes.

        The OS lock is cross-process on ordinary Windows/POSIX filesystems. Contention uses bounded
        non-blocking retries; unsupported locking or acquisition timeout fails closed, never entering
        thread-only while another process may own the run. Lock files live outside the run directory
        so delete/reset can hold the guard while moving or removing the run itself.

        `timeout` shortens (or lengthens) the acquisition budget for ONE caller without changing the
        service-wide `lock_acquire_timeout`. It exists for `_report_worker_crash`, which runs during
        worker teardown and holds the `.executing` claim while it waits; see that method for why
        giving up early is the safe direction there. Every ordinary caller omits it.
        """
        key = _lock_identity(rd)
        with self._local_lock:
            local = self._run_locks.setdefault(key, threading.RLock())
        budget = self.lock_acquire_timeout if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + budget
        if not local.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise HTTPException(503, "timed out waiting for the in-process run command sequencer")
        try:
            lock_path = self._sequence_path(rd)
            handle = open(lock_path, "a+")
            locked = False
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write("\0")
                        handle.flush()
                    while True:
                        handle.seek(0)
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            locked = True
                            break
                        except OSError as exc:
                            contention = exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                            if not contention:
                                raise HTTPException(
                                    503, f"run command locking is unsupported: {exc}") from exc
                            if time.monotonic() >= deadline:
                                raise HTTPException(
                                    503, "timed out waiting for the run command sequencer") from exc
                            time.sleep(min(0.05, self.poll_interval))
                else:
                    import fcntl
                    while True:
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            locked = True
                            break
                        except OSError as exc:
                            contention = isinstance(exc, BlockingIOError) or exc.errno in {
                                errno.EACCES, errno.EAGAIN}
                            if not contention:
                                raise HTTPException(
                                    503, f"run command locking is unsupported: {exc}") from exc
                            if time.monotonic() >= deadline:
                                raise HTTPException(
                                    503, "timed out waiting for the run command sequencer") from exc
                            time.sleep(min(0.05, self.poll_interval))
                yield
            finally:
                if locked:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()
        finally:
            local.release()

    def validate_paths(self, rd: Path) -> Path:
        """Return the canonical direct-child run, refusing writable sidecar symlinks.

        ``AppState.run_dir`` protects ordinary traversal, but an ``events.jsonl`` or ``.commands``
        symlink could otherwise turn an authenticated command into a write outside the run.  These
        are service-owned files, so unlike ``ui_meta.task_file`` there is no compatibility reason to
        permit indirection.
        """
        root = self.srv.root.resolve()
        canonical = rd.resolve()
        if canonical == root or canonical.parent != root:
            raise HTTPException(404, "no such run")
        events = canonical / "events.jsonl"
        if not events.exists():
            raise HTTPException(404, "no such run")
        try:
            if events.is_symlink() or events.resolve().parent != canonical:
                raise HTTPException(409, "run events.jsonl must not be a symlink")
        except OSError as exc:
            raise HTTPException(409, f"run event path cannot be validated: {exc}") from exc
        directory = canonical / ".commands"
        try:
            if directory.is_symlink() or (directory.exists() and directory.resolve().parent != canonical):
                raise HTTPException(409, "run .commands must not be a symlink")
        except OSError as exc:
            raise HTTPException(409, f"run command path cannot be validated: {exc}") from exc
        return canonical

    def _directory(self, rd: Path) -> Path:
        return self.validate_paths(rd) / ".commands"

    def _events_path(self, rd: Path) -> Path:
        return self.validate_paths(rd) / "events.jsonl"

    def _path(self, rd: Path, command_id: str) -> Path:
        if not _COMMAND_ID_RE.fullmatch(command_id):
            raise HTTPException(404, "no such command")
        path = self._directory(rd) / f"{command_id}.json"
        if path.is_symlink():
            raise HTTPException(409, "run command record must not be a symlink")
        return path

    def _exec_path(self, rd: Path, command_id: str) -> Path:
        path = self._directory(rd) / f".{command_id}.executing"
        if path.is_symlink():
            raise HTTPException(409, "run execution claim must not be a symlink")
        return path

    @staticmethod
    def _load(path: Path) -> Optional[dict]:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return row if isinstance(row, dict) else None

    @staticmethod
    def _save(path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record, indent=2, sort_keys=True, allow_nan=False)
        # Windows can deny ``os.replace`` for the few milliseconds another thread/process has the
        # destination open for a GET/read. The unique temp was already cleaned by atomic_write_text;
        # retry only the platform's transient sharing/access violations. Without this, observation
        # traffic can turn an otherwise-correct command into `command_worker_failed` even though its
        # marked intent is durable. Other errors remain immediate/fail-visible.
        for attempt in range(20):
            try:
                atomic_write_text(path, payload)
                return
            except PermissionError as exc:
                if (getattr(exc, "winerror", None) not in (5, 32)
                        and getattr(exc, "errno", None) not in (errno.EACCES, errno.EBUSY)):
                    raise
                if attempt == 19:
                    raise
                time.sleep(min(0.05, 0.002 * (attempt + 1)))

    @staticmethod
    def _public(record: dict) -> dict:
        hidden = {"data", "idempotency_key_digest", "payload_digest", "semantic_payload_digest",
                  "attached_semantic_payload_digest", "spawn_claim_released", "subject"}
        public = {key: value for key, value in record.items() if key not in hidden}
        data = record.get("data")
        generation = record.get("run_generation")
        hypothesis_id = data.get("id") if isinstance(data, dict) else None
        # Permanent deletion recovery must prove the exact semantic target before treating a terminal
        # receipt as its own. Expose only this closed, normalized identity — never the arbitrary command
        # payload or its secret-bearing idempotency material. Deriving it from the persisted immutable
        # data also upgrades pre-existing durable records without rewriting them. A malformed/legacy
        # record stays subject-less and clients therefore fail closed.
        if (record.get("event_type") == EV_HYPOTHESIS_UPDATED
                and isinstance(data, dict) and set(data) == {"id", "status"}
                and data.get("status") == "deleted"
                and isinstance(generation, str) and _RUN_GENERATION_RE.fullmatch(generation)
                and isinstance(hypothesis_id, str) and hypothesis_id == hypothesis_id.strip()
                and 0 < len(hypothesis_id) <= 256
                and not any(unicodedata.category(ch).startswith("C") for ch in hypothesis_id)):
            public["subject"] = {
                "kind": "hypothesis", "id": hypothesis_id, "status": "deleted",
            }
        return public

    # One walk of a run's durable command records (doc 25 SC-13). Five scanners had each opened the
    # directory, globbed, applied a symlink policy and loaded the record themselves — and the symlink
    # policies had ALREADY diverged between them, which is the failure worth removing: a symlinked
    # `cmd_*.json` is an attempt to make one run's command file point at another's, so a scanner that
    # forgets the check reads a record it does not own and answers a liveness question about the
    # wrong run.
    #
    # `on_symlink` names the two policies explicitly rather than leaving them implicit:
    #   * ``"refuse"`` — the four scanners that answer a question about a SPECIFIC record refuse the
    #     whole request, because a planted link means the answer cannot be trusted at all;
    #   * ``"unreadable"`` — `_active_command_ids`, which is a fail-CLOSED liveness census: it must
    #     count anything it cannot read as still active, so refusing would let a planted link block a
    #     destructive mutation's safety check instead of tripping it.
    def _scan_command_records(self, rd: Path, *, on_symlink: str):
        """Yield ``(path, record)`` per durable command file; ``record`` is None when unreadable."""
        directory = self._directory(rd)
        if not directory.exists():
            return
        for path in directory.glob("cmd_*.json"):
            if path.is_symlink():
                if on_symlink == "refuse":
                    raise HTTPException(409, "run command record must not be a symlink")
                yield path, None
                continue
            yield path, self._load(path)

    def _active_command_ids(self, rd: Path) -> list[str]:
        directory = self._directory(rd)
        if not directory.exists():
            return []
        active = []
        for path, record in self._scan_command_records(rd, on_symlink="unreadable"):
            # An unreadable durable record — malformed, or a planted symlink — is fail-closed:
            # destructive mutation must not erase the only evidence of a command whose state cannot
            # be determined.
            if record is None or record.get("status") not in TERMINAL_STATUSES:
                active.append(path.stem)
        for claim in directory.glob(".cmd_*.executing"):
            if claim.is_symlink():
                active.append(claim.name[1:-len(".executing")])
                continue
            try:
                # Positive owner death is conclusive immediately; age protects only ambiguous/live
                # owners from heartbeat pauses, not a PID the OS says no longer exists.
                owner_gone = self._execution_owner_definitely_gone(claim)
                if owner_gone:
                    claim.unlink()
                    continue
            except OSError:
                pass
            cid = claim.name[1:-len(".executing")]
            if cid not in active:
                active.append(cid)
        for claim in directory.glob(".activity_*.json"):
            if claim.is_symlink():
                active.append(claim.stem)
                continue
            try:
                if self._execution_owner_definitely_gone(claim):
                    claim.unlink()
                    continue
            except OSError:
                pass
            active.append(claim.stem)
        return sorted(active)

    def _unresolved_equivalent(self, rd: Path, event_type: str,
                               semantic_payload_digest: str) -> tuple[Optional[Path], Optional[dict]]:
        if event_type not in _RETRY_GUARDED_EVENTS:
            return None, None
        candidates = []
        for path, record in self._scan_command_records(rd, on_symlink="refuse"):
            if not record or record.get("event_type") != event_type:
                continue
            record_semantic = record.get("semantic_payload_digest")
            if not record_semantic:
                try:
                    _raw, record_semantic = self._payload(
                        event_type, dict(record.get("data") or {}))
                except HTTPException:
                    record_semantic = None
            if record_semantic != semantic_payload_digest:
                continue
            status = record.get("status")
            if status not in {"accepted", "executing", "failed", "timed_out"}:
                continue
            # accepted/executing is already one reserved logical command even in the tiny
            # reserve→append window. Failed/timed-out only block a new key if their intent became
            # durable; a pre-append validation/spawn failure is safe to correct under a new payload.
            if status in {"accepted", "executing"} or record.get("event_seq") is not None \
                    or self._find_intent(rd, str(record.get("id") or ""), record):
                candidates.append((float(record.get("created_at") or 0), path.name, path, record))
        if candidates:
            _created, _name, path, record = min(candidates, key=lambda item: (item[0], item[1]))
            return path, record
        return None, None

    def _finalize_incomplete(self, rd: Path, state=None,
                             observation: Optional[CommandObservation] = None) -> bool:
        """A finalize remains pending until its terminal projections are durably complete.

        Served from the INCREMENTAL observation, which is what that index exists for: this runs on
        every submit, every legacy /control POST and every destructive_guard entry, and it used to
        cost a fresh `EventStore(...).read_all()` plus a second full read+fold through
        `srv.state(rd)` each time. Both pieces are memoized on the observation's revision, so an
        unchanged log is free and a grown one parses only the new suffix. `observe()` re-stats the
        file on every call, so the view is never staler than the request that asked for it."""
        observation = observation or self._observe(rd)
        if observation.incomplete_finalize_scope() is not None:
            return True
        state = state or observation.state()
        return bool(state.finalization_pending() or (state.stop_requested and (
            not state.finished or str(state.stop_reason or "").lower() == "error")))

    def _pending_finalize_intent(
            self, rd: Path, observation: Optional[CommandObservation] = None):
        """Return the latest canonical external/legacy run_abort and its semantic digest."""
        observation = observation or self._observe(rd)
        event = observation.latest_run_abort
        if event is None:
            return None, None
        data = dict(event.data or {})
        data.pop("_command_id", None)
        try:
            normalized = _normalize_finalize_data(data)
            _raw, digest = self._payload(EV_RUN_ABORT, normalized)
        except HTTPException:
            return event, None
        return event, digest

    def _attached_finalize_intact(
            self, rd: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> bool:
        expected_seq = record.get("attached_event_seq")
        expected_digest = record.get("attached_semantic_payload_digest")
        if expected_seq is None or not expected_digest:
            return False
        observation = observation or self._observe(rd)
        latest, digest = self._pending_finalize_intent(rd, observation)
        if latest is None or latest.seq != expected_seq or digest != expected_digest:
            return False
        # The historical row may still exist after an external resume/superseding stop. Attachment
        # represents the effective pending finalize, not mere event ancestry.
        expected_reason = str((record.get("data") or {}).get("reason") or "")
        return str(observation.state().stop_requested or "") == expected_reason

    def _pending_finalize_record(self, rd: Path, semantic_payload_digest: Optional[str] = None
                                 ) -> tuple[Optional[Path], Optional[dict]]:
        """Find the durable finalize a reload/new browser key should observe, not duplicate."""
        finalize_incomplete = self._finalize_incomplete(rd)
        candidates = []
        for path, record in self._scan_command_records(rd, on_symlink="refuse"):
            if not record or record.get("event_type") != EV_RUN_ABORT:
                continue
            if (semantic_payload_digest is not None
                    and record.get("semantic_payload_digest", record.get("payload_digest"))
                    != semantic_payload_digest):
                continue
            if record.get("status") not in {"accepted", "executing", "failed", "timed_out"}:
                continue
            status = record.get("status")
            # accepted/executing is already the authoritative logical finalize even before its
            # worker appends. Failed/timed-out must have a durable/attached stop intent.
            if status in {"failed", "timed_out"}:
                if not finalize_incomplete:
                    continue
                if not record.get("attached") and record.get("event_seq") is None \
                        and self._find_intent(
                            rd, str(record.get("id") or ""), record) is None:
                    continue
            candidates.append((float(record.get("updated_at") or 0), path, record))
        if not candidates:
            return None, None
        _updated, path, record = max(candidates, key=lambda item: item[0])
        return path, record

    def _active_record(self, rd: Path) -> tuple[Optional[Path], Optional[dict]]:
        """Return the earliest reserved nonterminal command, including the pre-append window."""
        candidates = []
        for path, record in self._scan_command_records(rd, on_symlink="refuse"):
            if record is None:
                # A malformed/half-reserved record is an active unknown, so submission fails closed.
                record = {"id": path.stem, "status": "executing", "created_at": 0}
            if record.get("status") not in {"accepted", "executing"}:
                continue
            candidates.append((float(record.get("created_at") or 0), path.name, path, record))
        if not candidates:
            return None, None
        _created, _name, path, record = min(candidates, key=lambda item: (item[0], item[1]))
        return path, record

    def _unresolved_terminal_record(self, rd: Path) -> tuple[Optional[Path], Optional[dict]]:
        """Return the earliest retryable terminal command with an intact durable intent.

        ``failed``/``timed_out`` is only an observation result, not proof that an additive intent
        did not land.  The durable command may still reconcile from a late exact ack, or it may need
        an explicit same-id retry to drive the already-appended budget/fork/inject event.  A legacy
        caller has neither identity, so allowing it to append or spawn here would bypass that
        recovery boundary.

        Reconcile each candidate while the caller holds ``sequence(rd)``.  This deliberately does
        *not* block on rejected/pre-append/non-retryable failures, changed/missing intents, or a
        command whose late postcondition is now proven: those are safe terminal history, not a
        permanent compatibility lock.
        """
        candidates = []
        for path, record in self._scan_command_records(rd, on_symlink="refuse"):
            # Malformed and nonterminal records are handled fail-closed by _active_record.
            if record is None or record.get("status") not in {"failed", "timed_out"}:
                continue
            record = self._reconcile_observation(rd, path, record)
            if record.get("status") not in {"failed", "timed_out"}:
                continue
            if not bool((record.get("error") or {}).get("retryable")):
                continue
            command_id = str(record.get("id") or path.stem)
            if record.get("attached"):
                durable_intent = self._attached_finalize_intact(rd, record)
            else:
                durable_intent = self._find_intent(rd, command_id, record) is not None
            if not durable_intent:
                continue
            candidates.append((float(record.get("created_at") or 0), path.name, path, record))
        if not candidates:
            return None, None
        _created, _name, path, record = min(candidates, key=lambda item: (item[0], item[1]))
        return path, record

    @staticmethod
    def _reject_unresolved_reset(rd: Path, operation: str) -> None:
        """Fail closed while a durable whole-run Replay or deletion owns this namespace."""
        try:
            deletion = load_run_deletion_fence(rd)
        except RunDeletionStorageError as exc:
            raise HTTPException(503, {
                "code": "run_deletion_fence_unavailable",
                "message": f"Cannot {operation} because deletion ownership cannot be verified.",
                "remediation": "Inspect the saved deletion evidence before changing this run.",
            }) from exc
        if deletion is not None:
            raise HTTPException(410, {
                "code": "run_deletion_in_progress",
                "operation_id": deletion["operation_id"],
                "message": f"Cannot {operation} while deletion is unresolved.",
                "remediation": "Observe or retry that exact deletion operation first.",
            })
        try:
            marker = load_run_reset_marker(rd)
        except RunResetStorageError as exc:
            raise HTTPException(503, {
                "code": "run_reset_fence_unavailable",
                "message": f"Cannot {operation} because Replay ownership cannot be verified.",
                "remediation": "Inspect the saved Replay evidence before changing this run.",
            }) from exc
        if marker is not None:
            raise HTTPException(409, {
                "code": "run_reset_in_progress",
                "operation_id": marker["operation_id"],
                "message": f"Cannot {operation} while Replay is unresolved.",
                "remediation": "Observe or retry that exact Replay operation first.",
            })

    def reject_if_active(self, rd: Path, operation: str, *,
                         allow_incomplete_finalize: bool = False) -> None:
        """Fail closed when a legacy mutation would overtake a durable command intent.

        Caller must hold ``sequence(rd)`` so the check and its own append/spawn are one ordering
        boundary.
        """
        self._reject_unresolved_reset(rd, operation)
        pending_finalize = self._finalize_incomplete(rd)
        if pending_finalize and not allow_incomplete_finalize:
            raise HTTPException(409, {
                "code": "finalize_in_progress",
                "message": f"Cannot {operation} while terminal projections are incomplete.",
                "remediation": "Resume the finalization driver; do not append a legacy mutation.",
            })
        _path, active = self._active_record(rd)
        if active is not None:
            command_id = str(active.get("id") or "")
            raise HTTPException(409, {
                "code": "command_in_progress",
                "existing_command_id": command_id,
                "current_status": active.get("status"),
                "message": f"Cannot {operation} while another run command is in progress.",
                "remediation": f"GET /commands/{command_id} to a terminal status first.",
            })
        unresolved_path, unresolved = self._unresolved_terminal_record(rd)
        if unresolved is not None:
            command_id = str(unresolved.get("id") or (
                unresolved_path.stem if unresolved_path is not None else ""))
            raise HTTPException(409, {
                "code": "command_retry_required",
                "existing_command_id": command_id,
                "current_status": unresolved.get("status"),
                "message": (
                    f"Cannot {operation} while an earlier run command intent is unresolved."),
                "remediation": (
                    f"GET /commands/{command_id}; wait for late reconciliation or POST "
                    f"/commands/{command_id}/retry. Do not use a legacy mutation to bypass it."),
            })
        if self._recent_spawn_claim(rd):
            raise HTTPException(409, {
                "code": "engine_start_uncertain",
                "message": f"Cannot {operation} while an engine start is unresolved.",
                "remediation": "Wait for engine_running or definitive child exit; do not start another driver.",
            })

    @contextmanager
    def destructive_guard(self, rd: Path, operation: str):
        """Exclude submissions/workers while reset/delete performs an irreversible mutation."""
        # A paid UI call whose usage append failed deliberately retains a live activity claim and its
        # exact same-ID ledger. Give it one non-paid flush opportunity BEFORE taking ``sequence``:
        # successful flush closes the retained run_activity context, whose cleanup itself acquires
        # this sequencer. Calling the hook inside the block would deadlock. A persistent outage stays
        # fail-closed and the provider is never called by this accounting-only hook.
        flush_pending_cost = getattr(self.srv, "flush_pending_run_costs", None)
        if callable(flush_pending_cost):
            try:
                flushed = flush_pending_cost(rd)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - destructive mutation must fail closed
                raise HTTPException(
                    503, f"cannot {operation}: pending run-cost recovery failed") from exc
            if flushed is False:
                raise HTTPException(
                    409,
                    f"cannot {operation}: a paid call is waiting for durable run-cost accounting")
        with self.sequence(rd):
            try:
                deletion_fence = load_run_deletion_fence(rd)
            except RunDeletionStorageError as exc:
                raise HTTPException(503, {
                    "code": "run_deletion_fence_unavailable",
                    "message": f"Cannot {operation} because deletion ownership cannot be verified.",
                }) from exc
            if deletion_fence is not None:
                raise HTTPException(410, {
                    "code": "run_deletion_in_progress",
                    "operation_id": deletion_fence["operation_id"],
                    "message": f"Cannot {operation} while deletion is unresolved.",
                    "remediation": "Observe or retry that exact deletion operation first.",
                })
            try:
                reset_marker = load_run_reset_marker(rd)
            except RunResetStorageError as exc:
                raise HTTPException(503, {
                    "code": "run_reset_fence_unavailable",
                    "message": f"Cannot {operation} because Replay ownership cannot be verified.",
                }) from exc
            if reset_marker is not None:
                raise HTTPException(409, {
                    "code": "run_reset_in_progress",
                    "operation_id": reset_marker["operation_id"],
                    "message": f"Cannot {operation} while Replay is unresolved.",
                    "remediation": "Observe or retry that exact Replay operation first.",
                })
            # The probe set and its order are `durable_op.refuse_unless_quiescent`'s (doc 25 SC-06),
            # shared with Replay and deletion so a rung cannot be dropped from one destructive path
            # alone. The messages stay here: they are this guard's contract, and unlike the other two
            # callers they name the caller's own `operation`.
            refuse_unless_quiescent(
                self, rd,
                active_command=lambda active: HTTPException(
                    409, f"cannot {operation}: run has active command(s) {', '.join(active[:3])}; wait for a terminal status"),
                engine_start=lambda: HTTPException(
                    409, f"cannot {operation}: an engine start is still in progress; wait for its lock/status"),
                finalize_incomplete=lambda: HTTPException(
                    409, f"cannot {operation}: terminal projections are incomplete; resume finalization first"))
            # Re-check the canonical path while holding the sequencer.  A run symlink swapped after
            # the route's initial run_dir() lookup must not redirect a destructive operation.
            canonical = self.srv.run_dir(rd.name)
            if canonical != rd.resolve():
                raise HTTPException(409, f"cannot {operation}: run path changed during validation")
            yield canonical

    def begin_or_resume_deletion(
            self, rd: Path, *, operation_id: str, expected_generation: str,
            expected_seq: int) -> dict:
        """Shared exact deletion entry point used by HTTP and Assistant control."""
        from looplab.serve.deletion_service import begin_or_resume_run_deletion
        return begin_or_resume_run_deletion(
            self.srv, rd.name, operation_id=operation_id,
            expected_generation=expected_generation, expected_seq=expected_seq)

    def get_deletion(self, rd: Path, operation_id: str) -> dict:
        from looplab.serve.deletion_service import get_run_deletion
        return get_run_deletion(self.srv, rd.name, operation_id)

    @staticmethod
    def _payload(event_type: str, data: dict) -> tuple[bytes, str]:
        try:
            raw = json.dumps({"type": event_type, "data": data}, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"command payload is not valid JSON: {exc}") from exc
        return raw, hashlib.sha256(raw).hexdigest()

    def _read_existing(self, path: Path) -> Optional[dict]:
        # New writers publish the complete record with atomic replacement while holding the run
        # sequencer. Heal a pre-upgrade empty O_EXCL reservation once it is old enough that its owner
        # cannot still hold this same sequencer; no intent/worker could start before the record save.
        if not path.exists():
            return None
        for _ in range(20):
            row = self._load(path)
            if row is not None:
                return row
            time.sleep(0.01)
        try:
            if path.stat().st_size == 0 and time.time() - path.stat().st_mtime > 1.0:
                path.unlink()
                return None
        except OSError:
            pass
        raise HTTPException(503, "command record is temporarily unavailable")

    def _check_duplicate(self, record: dict, key_digest: str, payload_digest: str) -> dict:
        if record.get("idempotency_key_digest") != key_digest:
            raise HTTPException(409, "idempotency command-id collision")
        if record.get("payload_digest") != payload_digest:
            raise HTTPException(409, "Idempotency-Key was already used with a different command payload")
        return record

    def _record_generation_match(self, rd: Path, record: dict) -> tuple[Optional[bool], str]:
        """Return True/False for a comparable record token, None for unbound legacy evidence."""
        stored = record.get("run_generation")
        if not isinstance(stored, str) or _RUN_GENERATION_RE.fullmatch(stored) is None:
            return None, self.run_generation(rd)
        current = self.run_generation(rd)
        return stored.lower() == current, current

    @staticmethod
    def _generation_changed_error(record: dict, current: str) -> dict:
        error = _error(
            "run_generation_changed",
            "The command belongs to an event-log generation that no longer occupies this run id.",
            "Observe this record only; refresh the run and form a new command for its current generation.",
            retryable=False,
        )
        error["expected_generation"] = record.get("run_generation")
        error["current_generation"] = current or None
        return error

    @staticmethod
    def _generation_unavailable_error(current: str) -> dict:
        error = _error(
            "run_generation_unavailable",
            "The legacy command record has no trustworthy event-log generation binding.",
            "Observe this record only; refresh the run and form a new generation-bound command.",
            retryable=False,
        )
        error["current_generation"] = current or None
        return error

    def _record_generation_error(self, record: dict, match: Optional[bool], current: str) -> dict:
        if match is False:
            return self._generation_changed_error(record, current)
        return self._generation_unavailable_error(current)

    def _terminal(self, path: Path, record: dict, status: str, *, error: Optional[dict] = None) -> dict:
        record = dict(record)
        record["status"] = status
        record["updated_at"] = time.time()
        if error is not None:
            record["error"] = error
        elif status in {"succeeded", "noop"}:
            record["error"] = None
        self._save(path, record)
        return record

    def _succeeded(self, rd: Path, path: Path, record: dict) -> dict:
        # Exact ack / terminal postcondition proves the spawned process passed its startup window.
        # Release only this command's lease so an immediate next command/finalize-resume is not held
        # behind a stale Popen claim; external/reset and other-command leases remain untouched.
        self._clear_spawn_claim(rd, str(record.get("id") or ""))
        return self._terminal(path, record, "succeeded")

    def _reconcile_observation(
            self, rd: Path, path: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> dict:
        """Promote a failed/timed-out record if its durable postcondition arrived later.

        RECONCILIATION is observation-only: promoting or failing a TERMINAL record never appends or
        spawns.  (`get` itself is not: a nonterminal record whose worker died is restarted from here —
        see its own note — which is the crash-recovery path, and that worker does append/Popen.)  A
        same-key POST may explicitly retry the existing command below; it reuses the marked intent and
        therefore cannot double-apply an additive budget/fork/inject request.
        """
        observation = observation or self._observe(rd)
        marked_invalid = (not record.get("attached") and record.get("event_seq") is not None
                          and self._find_intent(
                              rd, str(record.get("id") or ""), record, observation) is None)
        attached_invalid = bool(record.get("attached")
                                and not self._attached_finalize_intact(rd, record, observation))
        if marked_invalid or attached_invalid:
            return self._terminal(path, record, "failed", error=_error(
                "command_intent_missing",
                "the durable command record points to an intent that is missing or changed",
                "do not retry automatically; inspect/repair the event log and command record",
                retryable=False))
        status = record.get("status")
        if status not in {"failed", "timed_out"}:
            return record
        spec = CONTROL_SPECS.get(str(record.get("event_type") or ""))
        if spec is None:
            return record
        if self._postcondition(rd, record, observation):
            updated = dict(record)
            updated["reconciled_from"] = status
            return self._succeeded(rd, path, updated)
        if ((record.get("error") or {}).get("code") == "engine_start_uncertain"
                and not self._recent_spawn_claim(rd)):
            # GET remains observation-only: it does not restart anything. It merely makes the same
            # command explicitly retryable once lock evidence or definitive PID death removes the
            # duplicate-Popen hazard.
            updated = dict(record)
            updated["error"] = _error(
                "postcondition_timeout",
                f"command intent was recorded but {record.get('postcondition')} was not observed in time",
                "POST this command id's /retry endpoint; the prior engine start is no longer unresolved",
                retryable=True)
            updated["updated_at"] = time.time()
            self._save(path, updated)
            return updated
        return record

    def _safe_retry(self, rd: Path, path: Path, record: dict) -> dict:
        """Re-arm the SAME command id/key; never mint or append a second logical intent."""
        observation = self._observe(rd)
        record = self._reconcile_observation(rd, path, record, observation)
        if record.get("status") not in {"failed", "timed_out"}:
            return record
        updated = dict(record)
        updated["status"] = "accepted"
        updated["error"] = None
        updated["updated_at"] = time.time()
        updated["deadline_at"] = time.time() + self.command_timeout
        updated["absolute_deadline_at"] = time.time() + self.max_observation_timeout
        updated["observe_after_seq"] = observation.latest_seq
        updated["retry_count"] = int(updated.get("retry_count", 0)) + 1
        # A prior spawn no longer proves this retry has a driver.  Recovery must observe fresh domain
        # progress or claim a new spawn under the per-run sequencer.
        updated["spawned_by_command"] = False
        updated.pop("spawn_claim_released", None)
        updated.pop("engine_pid", None)
        updated.pop("startup_slow", None)
        updated.pop("waiting_for_spawn", None)
        self._save(path, updated)
        return updated

    @staticmethod
    def _collaboration_precondition(state, event_type: str, data: dict,
                                    envelope: Optional[tuple] = None) -> Optional[dict]:
        """Recheck the exact semantic subject immediately before a collaboration append.

        `envelope` is the pre-computed `_card_resource_envelope()` tuple; the bounded-CAS caller passes
        it so a resource-pin re-check does not re-spawn nvidia-smi on every log-race retry. When omitted
        (direct callers/tests) the envelope is queried on demand.

        The per-event rule is `ControlSpec.precondition` (doc 25 SC-02).  This used to be a second
        if/elif chain over the same event types whose final `else` was the COMMENT recheck, so any
        type it did not name — a new collaboration event, or a corrupted record — was silently
        rechecked against a comment id it does not have.  A missing handler is now a refusal, and
        the registry's cross-check against `COLLABORATION_EVENTS` keeps that branch unreachable.
        """
        spec = CONTROL_SPECS.get(event_type)
        handler = spec.precondition if spec is not None else None
        if handler is None:
            return _error(
                "collaboration_precondition_missing",
                f"{event_type} has no append-time collaboration precondition",
                "do not retry; this command type is not an operator collaboration mutation")
        return handler(state, event_type, data, envelope)

    def _append_collaboration_intent(self, rd: Path, record: dict, event_data: dict
                                     ) -> tuple[Optional[Event], int, Optional[dict]]:
        """Strict-lock, bounded-CAS append for a collaboration mutation.

        Engine/domain events may advance the shared log between our read and append.  Retry those
        unrelated tail races after refolding; reject only when the exact semantic subject moved.
        """
        store = EventStore(self._events_path(rd))
        baseline = -1
        # Capture the GPU free-memory envelope ONCE at admission (only for a resource pin, its sole
        # consumer) rather than re-spawning the uncached nvidia-smi query on every CAS retry: the bounded
        # loop below retries only for LOG races (concurrent event appends, which do not change GPU memory)
        # and its retry window is milliseconds. This is a point-in-time snapshot, NOT a live per-attempt
        # reading — another process can still allocate/free VRAM between this probe and the tail append;
        # the pin is advisory and the reservation path re-clamps against real capacity when it runs.
        # Through the MODULE, not a by-value `from ... import`: this probe's OTHER consumer is the
        # intake normalizer in `control_validation`, and a test that shrinks the envelope between
        # admission and this re-check patches ONE name. A by-value binding here would leave that
        # patch reaching only half the pair — the half that then silently kept the real GPU probe.
        envelope = (control_validation._card_resource_envelope()
                    if record.get("event_type") == EV_CARD_RESOURCE_PINNED else None)
        for _ in range(8):
            events = store.read_all()
            current_generation = run_generation_token(events)
            if current_generation != record.get("run_generation"):
                error = self._generation_changed_error(record, current_generation)
                return None, (events[-1].seq if events else -1), error
            state = fold(events)
            error = self._collaboration_precondition(
                state, str(record.get("event_type") or ""), event_data, envelope=envelope)
            if error is not None:
                return None, (events[-1].seq if events else -1), error
            baseline = events[-1].seq if events else -1
            try:
                intent = store.append(
                    str(record["event_type"]), event_data,
                    expected_last_seq=baseline, require_lock=True)
                return intent, baseline, None
            except EventStoreConcurrencyError:
                continue
            except EventStoreLockError as exc:
                return None, baseline, _error(
                    "event_lock_unavailable", str(exc),
                    "restore cross-process file locking, then retry this exact command id",
                    retryable=True)
        return None, baseline, _error(
            "collaboration_concurrency_busy",
            "the event log kept changing while the collaboration subject was being verified",
            "retry this exact command id after the run produces less event traffic",
            retryable=True)

    def _standing_hint_duplicate(self, rd: Path, event_type: str, data: Optional[dict]) -> bool:
        if event_type != EV_HINT or (data or {}).get("replace"):
            return False
        incoming = standing_hint_dedup_key(data)
        return any(
            isinstance(hint, dict)
            and standing_hint_dedup_key(hint) == incoming
            for hint in self.srv.state(rd).pending_hints
        )

    def _decision(self, rd: Path, event_type: str) -> tuple[str, Optional[dict]]:
        """Choose what this command does to the engine: append / attach / noop / reject.

        The per-event rule is `ControlSpec.decide`, which returns None when the shared engine-policy
        tail below already answers correctly for that event (doc 25 SC-02).
        """
        # Command-only collaboration never requires STARTING a driver. A live driver may observe an
        # intent (notably operator Card drop), but the strict append lock is the only ownership
        # guarantee this write needs, even when engine.lock diagnostics are degraded.
        if event_type in COLLABORATION_EVENTS:
            return "append", None
        state = self.srv.state(rd)
        liveness = self._engine_state(rd)
        if liveness is None:
            return "reject", self._engine_unknown_error(f"apply {event_type}")
        alive = liveness is True
        pending_finalize = self._finalize_incomplete(rd, state)

        spec = CONTROL_SPECS[event_type]
        if spec.decide is not None:
            settled = spec.decide(self, rd, event_type, state, alive, pending_finalize)
            if settled is not None:
                return settled
        if pending_finalize and spec.engine_policy is not EnginePolicy.NO_SPAWN:
            return "reject", _error(
                "finalize_in_progress", f"cannot apply {event_type} while finalization is pending",
                "wait for finalization to finish before submitting engine-driving work",
                retryable=True)
        if state.finished and alive and spec.engine_policy is EnginePolicy.ENSURE_RUNNING:
            return "reject", _error(
                "engine_finishing", "the engine is still completing its terminal write-out",
                "retry after engine_running becomes false", retryable=True)
        return "append", None

    def submit(self, rd: Path, idempotency_key: str, event_type: str, data,
               *, expected_generation: object = None) -> dict:
        key = str(idempotency_key or "")
        if not key or len(key) > 512:
            raise HTTPException(400, "Idempotency-Key is required and must be at most 512 characters")
        if not isinstance(event_type, str):
            raise HTTPException(400, "command type must be a string")
        raw_data = {} if data is None else data
        if not isinstance(raw_data, dict):
            raise HTTPException(400, "command data must be a JSON object")
        _raw, payload_digest = self._payload(event_type, raw_data)
        # The precondition itself is a strict wire contract even for an idempotent replay. A valid
        # stale token may resolve an existing same-key record below, but missing/malformed input must
        # never be silently accepted just because a record happens to exist.
        expected = _normalize_expected_generation(expected_generation)
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        command_id = "cmd_" + key_digest[:32]
        path = self._path(rd, command_id)

        should_start = False
        synchronous = False
        with self.sequence(rd):
            existing = self._read_existing(path)
            if existing is not None:
                record = self._check_duplicate(existing, key_digest, payload_digest)
                # A terminal same-key record is immutable observation history and remains readable
                # across Replay. A nonterminal record would restart a worker below, so it is a write
                # path and must honor the same namespace fence as a genuinely new command.
                if record.get("status") not in TERMINAL_STATUSES:
                    self._reject_unresolved_reset(rd, "resume this run command")
                generation_match, current_generation = self._record_generation_match(rd, record)
                if generation_match is not True:
                    if record.get("status") not in TERMINAL_STATUSES:
                        # An externally replaced log or crash-recovery edge can leave an old
                        # nonterminal record even though normal reset guards exclude it. Make it
                        # observable but inert; GET/same-key POST must never wake a worker in B.
                        record = self._terminal(
                            path, record, "failed",
                            error=self._record_generation_error(
                                record, generation_match, current_generation))
                    # A terminal A record remains byte-for-byte semantic history. Do not reconcile
                    # its intent/postcondition against B and rewrite a successful lost-response
                    # replay as command_intent_missing.
                else:
                    record = self._reconcile_observation(rd, path, record)
            else:
                # Idempotency lookup intentionally wins over this precondition: after an in-place
                # reset, replaying a lost response with the SAME key/payload must resolve the old
                # durable record, never reject it or apply it to the replacement generation. Only a
                # genuinely brand-new record is bound to the generation observed by its caller.
                self._reject_unresolved_reset(rd, "submit a new run command")
                current_generation = self.run_generation(rd)
                if not current_generation:
                    raise HTTPException(409, {
                        "code": "run_generation_unavailable",
                        "message": "The run has no durable generation identity yet.",
                        "remediation": (
                            "Wait for run_started, refresh GET /state, and submit a new command with "
                            "the returned generation."),
                    })
                if expected != current_generation:
                    raise HTTPException(409, {
                        "code": "run_generation_changed",
                        "expected_generation": expected,
                        "current_generation": current_generation,
                        "message": "The run was reset or replaced after this command was formed.",
                        "remediation": (
                            "Refresh the run, review its current state, and form a new command with "
                            "a new idempotency key and current generation."),
                    })
                normalized_candidate = None
                semantic_candidate = None
                normalization_error = None
                gate_field = ({EV_APPROVAL_GRANTED: "approval_request_seq",
                               EV_SPEC_APPROVED: "spec_approval_request_seq"}.get(event_type))
                gate_before = (getattr(self.srv.state(rd), gate_field, None)
                               if gate_field is not None else None)
                try:
                    normalized_candidate = normalize_control(
                        self.srv, rd, event_type, raw_data)
                    if gate_field is not None:
                        gate_after = getattr(self.srv.state(rd), gate_field, None)
                        if (not isinstance(gate_before, int) or isinstance(gate_before, bool)
                                or gate_before < 0 or gate_after != gate_before):
                            raise HTTPException(409, {
                                "code": "approval_state_changed",
                                "message": "the approval request changed while the command was admitted",
                                "remediation": "refresh the run and submit a new approval command",
                                "retryable": False,
                            })
                    _semantic_raw, semantic_candidate = self._payload(
                        event_type, normalized_candidate)
                except HTTPException as exc:
                    normalization_error = exc
                # Reload recovery is special for FINALIZE: the browser may have lost its generated
                # key/command id, while the durable stop intent is still pending. Return the existing
                # record so it can resume polling; never mint an alias record, event, or second driver.
                reattach_path = reattach = None
                if event_type == EV_RUN_ABORT and semantic_candidate is not None:
                    reattach_path, reattach = self._pending_finalize_record(
                        rd, semantic_candidate)
                    if reattach is None:
                        _other_path, other_finalize = self._pending_finalize_record(rd)
                        if other_finalize is not None:
                            existing_id = str(other_finalize.get("id") or "")
                            raise HTTPException(409, {
                                "code": "finalize_payload_conflict",
                                "existing_command_id": existing_id,
                                "message": "A finalize with different normalized data is unresolved.",
                                "remediation": f"GET /commands/{existing_id}; do not alias another reason.",
                            })
                if reattach is not None and reattach_path is not None:
                    path = reattach_path
                    record = self._reconcile_observation(rd, path, reattach)
                else:
                    if event_type not in COLLABORATION_EVENTS and self._recent_spawn_claim(rd):
                        raise HTTPException(409, {
                            "code": "engine_start_uncertain",
                            "message": "An earlier engine start has not exposed its lock or exited.",
                            "remediation": (
                                "Wait for engine_running or definitive child exit; do not submit "
                                "another state-changing command."),
                        })
                    # A fresh key must not double-apply any unresolved control intent. Unlike
                    # finalize recovery above, the caller must name and explicitly retry the original
                    # failed/timed-out command; a silent alias would hide which intent is authoritative.
                    equivalent_path = equivalent = None
                    if semantic_candidate is not None:
                        equivalent_path, equivalent = self._unresolved_equivalent(
                            rd, event_type, semantic_candidate)
                    if equivalent is not None and equivalent_path is not None:
                        existing_id = str(equivalent.get("id") or "")
                        raise HTTPException(
                            409, {
                                "code": "retry_existing_command",
                                "existing_command_id": existing_id,
                                "message": "An unresolved identical control intent already exists.",
                                "remediation": (
                                    f"GET /commands/{existing_id}; if it is retryable failed/timed_out, "
                                    f"POST /commands/{existing_id}/retry."),
                            })
                    # Serialize DRIVER commands only. This global in-progress gate must NOT block a
                    # collaboration-only control (e.g. an operator Card drop): while a long engine command
                    # remains `executing` — a pause whose postcondition waits out an in-flight eval —
                    # blocking the Card drop would keep _evaluate from ever seeing the event it watches to
                    # cancel its paid subprocess. Collaboration has its own strict-lock generation/subject/
                    # tail CAS in _append_collaboration_intent, so it stays serialized without this gate.
                    if event_type not in COLLABORATION_EVENTS:
                        _active_path, active = self._active_record(rd)
                        if active is not None:
                            existing_id = str(active.get("id") or "")
                            raise HTTPException(409, {
                                "code": "command_in_progress",
                                "existing_command_id": existing_id,
                                "current_status": active.get("status"),
                                "message": "Another state-changing run command is still in progress.",
                                "remediation": (
                                    f"GET /commands/{existing_id} to a terminal status before submitting "
                                    "the next command."),
                            })
                    now = time.time()
                    record = {
                        "id": command_id,
                        "status": "accepted",
                        "event_type": event_type,
                        "error": None,
                        "data": {},
                        "idempotency_key_digest": key_digest,
                        "payload_digest": payload_digest,
                        "run_generation": current_generation,
                        "created_at": now,
                        "updated_at": now,
                        "deadline_at": now + self.command_timeout,
                        "absolute_deadline_at": now + self.max_observation_timeout,
                        "driver_was_alive": (None if event_type in COLLABORATION_EVENTS
                                             else self._engine_state(rd)),
                    }
                    try:
                        if normalization_error is not None:
                            raise normalization_error
                        normalized = dict(normalized_candidate or {})
                        record["data"] = normalized
                        if gate_field is not None:
                            record["approval_gate_field"] = gate_field
                            record["approval_gate_seq"] = gate_before
                        _semantic_raw, record["semantic_payload_digest"] = self._payload(
                            event_type, normalized)
                        record["engine_policy"] = CONTROL_SPECS[event_type].engine_policy.value
                        record["postcondition"] = CONTROL_SPECS[event_type].postcondition
                        decision, err = self._decision(rd, event_type)
                        if (decision == "append"
                                and self._standing_hint_duplicate(rd, event_type, normalized)):
                            decision = "noop"
                        if decision == "reject":
                            record["status"] = "rejected"
                            record["error"] = err
                        elif decision == "noop":
                            record["status"] = "noop"
                        elif decision == "attach":
                            pending_event, pending_digest = self._pending_finalize_intent(rd)
                            if pending_event is None or not pending_digest:
                                record["status"] = "rejected"
                                record["error"] = _error(
                                    "command_intent_missing",
                                    "the pending external finalize intent is missing or malformed",
                                    "inspect/repair the event log; do not infer completion",
                                    retryable=False)
                            elif pending_digest != record["semantic_payload_digest"]:
                                record["status"] = "rejected"
                                record["error"] = _error(
                                    "finalize_payload_conflict",
                                    "a pending external finalize has different normalized data",
                                    "observe the existing finalize; do not alias another reason",
                                    retryable=False)
                            else:
                                record["attached"] = True
                                record["attached_event_seq"] = pending_event.seq
                                record["attached_semantic_payload_digest"] = pending_digest
                    except HTTPException as exc:
                        record["status"] = "rejected"
                        detail = exc.detail
                        if (isinstance(detail, dict) and detail.get("code")
                                and detail.get("message")):
                            safe_error = _error(
                                str(detail["code"]), str(detail["message"]),
                                str(detail.get("remediation") or ""),
                                retryable=bool(detail.get("retryable", False)))
                            # Expose only the bounded numeric CAS value needed to recover from a
                            # stale comment edit. Arbitrary exception-detail fields stay excluded.
                            current_version = detail.get("current_version")
                            if (isinstance(current_version, int)
                                    and not isinstance(current_version, bool)
                                    and 1 <= current_version <= COMMENT_MAX_VERSION):
                                safe_error["current_version"] = current_version
                            record["error"] = safe_error
                        else:
                            record["error"] = _error(
                                "invalid_command" if exc.status_code < 404 else "command_target_not_found",
                                str(detail),
                                "correct the command payload and submit it with a new idempotency key")

                    # The cross-process sequencer already excludes competing creators. Atomic replace
                    # publishes either no record or the complete record, never an immortal empty
                    # reservation after a process crash.
                    self._save(path, record)

            should_start = record.get("status") not in TERMINAL_STATUSES
            if should_start:
                spec = CONTROL_SPECS[str(record["event_type"])]
                synchronous = spec.engine_policy is EnginePolicy.NO_SPAWN and record["event_type"] != EV_PAUSE

        if should_start:
            if synchronous and self._claim_execution(rd, str(record["id"])):
                self._execute(rd, path, record, claimed=True)
            else:
                self._start_worker(rd, path, record)
        result = self._public(self._load(path) or record)
        # A collaboration append promises strict cross-process serialization. Surface the missing
        # guarantee as HTTP 503 (while retaining the durable command id for explicit same-intent
        # recovery) instead of returning an ordinary failed 200 record.
        if (event_type in COLLABORATION_EVENTS and result.get("status") == "failed"
                and (result.get("error") or {}).get("code") == "event_lock_unavailable"):
            detail = dict(result["error"])
            detail["command_id"] = result.get("id")
            raise HTTPException(503, detail)
        return result

    def retry(self, rd: Path, command_id: str) -> dict:
        path = self._path(rd, command_id)
        with self.sequence(rd):
            record = self._read_existing(path)
            if record is None:
                raise HTTPException(404, "no such command")
            generation_match, current_generation = self._record_generation_match(rd, record)
            if generation_match is not True:
                detail = self._record_generation_error(record, generation_match, current_generation)
                detail.update({
                    "existing_command_id": command_id,
                    "current_status": record.get("status"),
                })
                raise HTTPException(409, detail)
            record = self._reconcile_observation(rd, path, record)
            if record.get("status") == "succeeded":
                return self._public(record)
            self._reject_unresolved_reset(rd, "retry this run command")
            if (record.get("event_type") not in COLLABORATION_EVENTS
                    and self._recent_spawn_claim(rd)):
                raise HTTPException(409, {
                    "code": "engine_start_uncertain",
                    "existing_command_id": command_id,
                    "current_status": record.get("status"),
                    "message": "The prior detached engine may still be starting without a live lock.",
                    "remediation": "Wait for engine_running or definitive child exit; retry must not Popen yet.",
                })
            if (record.get("status") not in {"failed", "timed_out"}
                    or not bool((record.get("error") or {}).get("retryable"))):
                raise HTTPException(409, {
                    "code": "command_not_retryable",
                    "existing_command_id": command_id,
                    "current_status": record.get("status"),
                    "message": "Only retryable failed/timed_out commands can be retried.",
                    "remediation": f"GET /commands/{command_id} and observe its current status.",
                })
            # retry preserves the same concurrency contract as first admission. A
            # collaboration append may overtake a live driver command, but a driver retry may not.
            if record.get("event_type") not in COLLABORATION_EVENTS:
                _active_path, active = self._active_record(rd)
                if active is not None and str(active.get("id") or "") != command_id:
                    active_id = str(active.get("id") or "")
                    raise HTTPException(409, {
                        "code": "command_in_progress",
                        "existing_command_id": active_id,
                        "current_status": active.get("status"),
                        "message": "Another state-changing run command is still in progress.",
                        "remediation": (
                            f"GET /commands/{active_id} to a terminal status before retrying "
                            f"{command_id}."),
                    })
            record = self._safe_retry(rd, path, record)
        if record.get("event_type") in COLLABORATION_EVENTS \
                and self._claim_execution(rd, str(record["id"])):
            self._execute(rd, path, record, claimed=True)
        else:
            self._start_worker(rd, path, record)
        result = self._public(self._load(path) or record)
        if (record.get("event_type") in COLLABORATION_EVENTS and result.get("status") == "failed"
                and (result.get("error") or {}).get("code") == "event_lock_unavailable"):
            detail = dict(result["error"])
            detail["command_id"] = result.get("id")
            raise HTTPException(503, detail)
        return result

    # NOT side-effect-free, despite being a GET: a NONTERMINAL record here gets its worker
    # restarted, and that worker DOES append the marked intent and may Popen an engine. This is the
    # deliberate crash-recovery path — an accepted record whose worker died must become drivable
    # again by polling — and it is safe because the intent is marked (so the append cannot
    # double-apply) and the spawn claim serializes the Popen. Only TERMINAL-record reconciliation
    # (`_reconcile_observation`) is genuinely observation-only.
    def get(self, rd: Path, command_id: str) -> dict:
        path = self._path(rd, command_id)
        with self.sequence(rd):
            record = self._read_existing(path)
            if record is None:
                raise HTTPException(404, "no such command")
            generation_match, current_generation = self._record_generation_match(rd, record)
            if generation_match is not True:
                if record.get("status") not in TERMINAL_STATUSES:
                    record = self._terminal(
                        path, record, "failed",
                        error=self._record_generation_error(
                            record, generation_match, current_generation))
                # Terminal cross-generation/legacy records are observation-only history.
            else:
                record = self._reconcile_observation(rd, path, record)
            if record.get("status") not in TERMINAL_STATUSES:
                self._reject_unresolved_reset(rd, "resume this run command")
        if record.get("status") not in TERMINAL_STATUSES:
            self._start_worker(rd, path, record)
            record = self._load(path) or record
        return self._public(record)

    def recover_pending_restarts(self) -> None:
        """Restart nonterminal restart-command workers after a UI-server process loss.

        A restart record can be durable a few instructions before its folded intent. Scanning only
        this compound lifecycle command closes that reserve->append crash window; once the intent is
        present, the independent resume reconciler is the second recovery path. Cross-process worker
        claims keep multiple uvicorn startup hooks idempotent.
        """
        try:
            candidates = list(self.srv.root.iterdir()) if self.srv.root.exists() else []
        except OSError:
            return
        for candidate in candidates:
            try:
                rd = self.validate_paths(candidate)
                directory = rd / ".commands"
                paths = list(directory.glob("cmd_*.json")) if directory.exists() else []
            except (HTTPException, OSError):
                continue
            for path in paths:
                try:
                    if path.is_symlink() or not _COMMAND_ID_RE.fullmatch(path.stem):
                        continue
                    with self.sequence(rd):
                        record = self._read_existing(path)
                        if (record is None or record.get("event_type") != EV_RESTART
                                or record.get("status") in TERMINAL_STATUSES):
                            continue
                        generation_match, current_generation = self._record_generation_match(
                            rd, record)
                        if generation_match is not True:
                            self._terminal(
                                path, record, "failed",
                                error=self._record_generation_error(
                                    record, generation_match, current_generation))
                            continue
                    self._start_worker(rd, path, record)
                except (HTTPException, OSError):
                    # One malformed/unavailable run must not prevent recovery of every other run.
                    continue

    def _claim_execution(self, rd: Path, command_id: str) -> bool:
        lock = self._exec_path(rd, command_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        owner = {"pid": os.getpid(), "created_at": time.time()}
        try:
            identity = self.process_identity(os.getpid())
        except Exception:  # noqa: BLE001 - identity is an optional hardening token
            identity = None
        if identity:
            owner["process_identity"] = identity

        def publish() -> bool:
            # Publish a fully-written inode with one exclusive hard-link CAS. A hard kill can leave
            # an unreferenced temp, but never an empty/partial authoritative `.executing` claim.
            temp = lock.with_name(f".{lock.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
            try:
                self._save(temp, owner)
                try:
                    os.link(temp, lock)
                    return True
                except FileExistsError:
                    return False
                except OSError:
                    if lock.exists():
                        return False
                    # Some network/FAT filesystems cannot hard-link. Preserve functionality with a
                    # short O_EXCL write; any kill inside this fallback is recoverable through the
                    # explicit active-claim resolver rather than becoming a permanent deadlock.
                    # Its two load-bearing properties — exclusivity under a genuine create race, and
                    # no orphaned claim after a failed write — are pinned by
                    # tests/test_run_command_service.py::
                    # test_execution_claim_falls_back_to_o_excl_where_hard_links_are_unsupported.
                    try:
                        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    except FileExistsError:
                        return False
                    try:
                        raw = json.dumps(owner, sort_keys=True).encode("utf-8")
                        written = 0
                        while written < len(raw):
                            written += os.write(fd, raw[written:])
                        try:
                            os.fsync(fd)
                        except OSError:
                            pass
                    except BaseException:
                        try:
                            lock.unlink()
                        except OSError:
                            pass
                        raise
                    finally:
                        os.close(fd)
                    return True
            finally:
                try:
                    temp.unlink()
                except OSError:
                    pass

        if not publish():
            # A missed heartbeat can mean suspension, not death, so reclaim needs POSITIVE evidence
            # that the owning process exited or its PID was reused — and that evidence is conclusive
            # immediately, exactly as `_active_command_ids` states ("age protects only ambiguous/live
            # owners from heartbeat pauses, not a PID the OS says no longer exists"). An additional
            # `time.time() - lock.stat().st_mtime <= command_timeout + 30 and not owner_gone` fence
            # used to sit here; it could only fire when `not owner_gone`, which the check below
            # already covers unconditionally, so it never changed an outcome while implying a
            # deadline this path does not wait for.
            try:
                owner_gone = self._execution_owner_definitely_gone(lock)
                if not owner_gone:
                    return False
                lock.unlink()
                if not publish():
                    return False
            except (OSError, FileExistsError):
                return False
        return True

    def _release_execution(self, rd: Path, command_id: str, *,
                           record_path: Optional[Path] = None) -> None:
        """Drop this worker's `.executing` claim. Best-effort, and it must never raise.

        OSError was not the only way this fails. `_exec_path` → `_directory` → `validate_paths`
        raises HTTPException — 404 once the run's `events.jsonl` has vanished under a delete/reset,
        409 for a sidecar that turned into a symlink — and both halves of that hurt:

        * it escaped `_execute`'s `finally`, ending the worker thread mid-teardown; on the
          synchronous `get`/`retry` call paths, which run `_execute` inline for collaboration events,
          it also replaced the command record with a bare 404;
        * and the claim survived. `_active_command_ids` counts a `.executing` file whose owner PID is
          still alive as an ACTIVE command, so a claim this server can no longer address goes on
          blocking every later reset/delete of that run until the process exits.

        `record_path` closes the second half without a second copy of the path policy — the divergence
        `_scan_command_records` exists to remove. The claim is the record file's sibling, and the
        caller's record path was already resolved through `validate_paths`; only the event-log
        existence check, which has nothing to do with removing our own claim, is skipped. Callers that
        hold that path pass it; the rest keep the lookup.
        """
        try:
            claim = (self._exec_path(rd, command_id) if record_path is None
                     else record_path.with_name(f".{command_id}.executing"))
            if claim.is_symlink():      # `_exec_path`'s own refusal, kept for the sibling spelling
                return
            claim.unlink()
        except (OSError, ValueError, HTTPException):
            pass

    def _heartbeat_execution(self, rd: Path, command_id: str) -> None:
        try:
            os.utime(self._exec_path(rd, command_id), None)
        except OSError:
            pass

    def _start_worker(self, rd: Path, path: Path, record: dict) -> None:
        command_id = str(record.get("id") or "")
        if not self._claim_execution(rd, command_id):
            return
        thread = threading.Thread(
            target=self._execute, args=(rd, path, record), kwargs={"claimed": True},
            daemon=True, name=f"looplab-{command_id[:20]}")
        try:
            thread.start()
        except BaseException:
            # A live-owner claim with no worker would otherwise block recovery until PID reuse/death.
            self._release_execution(rd, command_id, record_path=path)
            raise

    def _observe(self, rd: Path) -> CommandObservation:
        return self._command_observations.observe(self._events_path(rd))

    def _find_intent(
            self, rd: Path, command_id: str, record: Optional[dict] = None,
            observation: Optional[CommandObservation] = None):
        """Return the one exact marked intent, not merely any event carrying the marker.

        The record's sequence, event type, and normalized semantic payload are all part of durable
        command identity. Log repair/rewrite that preserves only ``_command_id`` must never satisfy a
        folded-intent postcondition or make a stale command_ack look causal.
        """
        observation = observation or self._observe(rd)
        event = observation.marked_intent(command_id)
        if event is None:
            return None
        if record is None:
            return event
        expected_seq = record.get("event_seq")
        if expected_seq is not None and event.seq != expected_seq:
            return None
        event_type = str(record.get("event_type") or "")
        if event.type != event_type:
            return None
        actual_data = dict(event.data or {})
        actual_data.pop("_command_id", None)
        expected_digest = record.get("semantic_payload_digest")
        if expected_digest:
            try:
                _raw, actual_digest = self._payload(event_type, actual_data)
            except HTTPException:
                return None
            if actual_digest != expected_digest:
                return None
        elif actual_data != (record.get("data") or {}):
            return None
        return event

    @staticmethod
    def _observe_after(record: dict) -> int:
        return int(record.get(
            "observe_after_seq", record.get("event_seq", record.get("baseline_seq", -1))))

    def _domain_failure(
            self, rd: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> Optional[dict]:
        after = self._observe_after(record)
        observation = observation or self._observe(rd)
        event = observation.domain_failure_after(after)
        if event is not None:
            detail = str((event.data or {}).get("error") or "engine exited with an error")
            return _error(
                "engine_failed", detail[:500],
                "correct the run error, then POST this command id's /retry endpoint",
                retryable=True)
        return None

    def _spawn(self, rd: Path) -> Optional[int]:
        task_file = task_file_for(rd)
        if not task_file:
            raise RuntimeError("run has no task.snapshot.json or usable ui_meta.json")
        # The CLI's resume path is stop-aware: it preserves a pending run_abort and appends EV_RESUME
        # only for ordinary paused/finished continuation.  Never append run_reopened here.
        popen_boundary_entered = False
        try:
            with self.srv.settings.launch_env_for_run(rd) as secret_env:
                # This is the conservative process boundary: an injected spawner can fail either
                # before or after the OS accepted Popen, and context release can fail after return.
                popen_boundary_entered = True
                return self.spawn_engine(
                    ["resume", str(rd), "--task-file", str(task_file)],
                    env=secret_env, run_dir=rd)
        except BaseException as exc:
            if popen_boundary_entered:
                raise EngineSpawnOutcomeUnknown(
                    "engine process creation may have succeeded") from exc
            raise

    def _claim_restart_spawn(self, rd: Path) -> bool:
        """Claim and spawn the replacement owner for a folded restart, never the old owner.

        The caller invokes this only after observing the singleton as definitively free. The shared
        lifecycle helper rechecks that fact under the reset/delete fence, appends a durable launch
        claim, then performs Popen. If this command worker disappears before any of those steps, the
        restart event remains a normal pending resume for the server-startup reconciler.
        """
        task_file = task_file_for(rd)
        if not task_file:
            raise RuntimeError("run has no task.snapshot.json or usable ui_meta.json")
        return _claim_and_spawn_resume(
            rd,
            ["resume", str(rd), "--task-file", str(task_file)],
            cancel_event=getattr(self.srv, "resume_cancel", None),
            wait_on_alive=False,
            spawn_engine=self.spawn_engine,
            liveness=self._engine_state,
            launch_env=lambda: self.srv.settings.launch_env_for_run(rd),
        )

    def _postcondition(
            self, rd: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> bool:
        observation = observation or self._observe(rd)
        kind = record.get("postcondition")
        if (record.get("attached")
                and not self._attached_finalize_intact(rd, record, observation)):
            return False
        if (not record.get("attached") and record.get("event_seq") is not None
                and self._find_intent(
                    rd, str(record.get("id") or ""), record, observation) is None):
            return False
        if kind == "folded_intent":
            intent = self._find_intent(rd, str(record["id"]), record, observation)
            if intent is None:
                return False
            observation.state()  # prove the complete log, including the marked intent, still folds
            return True
        if kind == "paused_and_stopped":
            state = observation.state()
            return bool(state.paused and self._engine_state(rd) is False)
        if kind == "restart_served":
            state = observation.state()
            event_seq = record.get("event_seq")
            # ``resume_served`` is written only after the replacement CLI owns engine.lock. Requiring
            # it to be later than THIS marked restart prevents an old owner/startup from satisfying a
            # new command; requiring the pause to be lifted proves the full pause->resume transition.
            return bool(isinstance(event_seq, int) and not isinstance(event_seq, bool)
                        and state.last_resume_served_seq > event_seq and not state.paused)
        if kind == "finished_and_stopped":
            state = observation.state()
            if (not state.finished or self._engine_state(rd) is not False
                    or str(state.stop_reason or "").lower() == "error"):
                return False
            if not state.stop_requested:
                return False
            # New-format engines publish this only after cost/reflection/read-model/trace/tree are
            # complete. A legacy terminal event has no explicit scope and stays backward compatible.
            if (observation.incomplete_finalize_scope() is not None
                    or state.finalization_pending()):
                return False
            # An attached record observes an external/legacy finalize rather than owning a marked
            # intent. Once replay says that same stop is non-error finished and the driver released
            # its lock, the effect is satisfied even if completion raced command-record creation.
            if record.get("attached"):
                return True
            # Do not let an old natural/error finish satisfy a newly attached finalize.  On retry,
            # ``observe_after_seq`` advances past the failed attempt, so success requires a fresh,
            # non-error run_finished causally after that boundary.
            after = self._observe_after(record)
            if observation.has_non_error_finish_after(after):
                return True
            # Decision→append race: natural completion can land after the preflight baseline but just
            # before this command's run_abort intent. It is still the terminal attempt this finalize
            # observed; requiring another finish would reopen/extend an already completed run.
            try:
                baseline = int(record.get("baseline_seq", -1))
            except (TypeError, ValueError, OverflowError):
                baseline = -1
            return (record.get("event_type") == EV_RUN_ABORT
                    and observation.has_non_error_finish_after(baseline))
        if kind == "engine_ack":
            command_id = str(record.get("id") or "")
            event_seq = record.get("event_seq")
            return observation.has_ack(command_id, event_seq)
        return False

    def _try_restart_claim(self, rd: Path, path: Path, record: dict) -> bool:
        """Claim the RESTART_AFTER_EXIT replacement launch. False = TERMINALIZED, caller returns.

        `_execute` performed this identically at admission and again in the monitor loop after a
        pre-existing engine died, so a change to the uncertain-boundary wording or the
        `replacement_launch_claimed` bookkeeping reached only one of them (doc 25 SC-07).
        """
        try:
            launched = self._claim_restart_spawn(rd)
        except Exception as exc:  # noqa: BLE001 - durable intent remains startup-recoverable
            uncertain = isinstance(exc, EngineSpawnOutcomeUnknown)
            self._terminal(path, record, "failed", error=_error(
                "resume_start_uncertain" if uncertain else "spawn_failed",
                ("replacement run engine creation crossed an uncertain process boundary"
                 if uncertain else f"could not start the replacement run engine: {exc}"),
                ("observe the durable resume launch claim; startup recovery may finish the "
                 "same intent, so do not submit another restart"
                 if uncertain else
                 "fix the cause; startup recovery or this command's retry can serve the same intent"),
                retryable=not uncertain))
            return False
        if launched:
            record["replacement_launch_claimed"] = True
            record["updated_at"] = time.time()
            self._save(path, record)
        return True

    def _spawn_under_claim(self, rd: Path, path: Path, record: dict, command_id: str,
                           *, restarting: bool) -> tuple[bool, Optional[int]]:
        """Lease → Popen → persist the PID. Returns ``(terminalized, pid)``; on True the caller returns.

        Write the lease *before* Popen. If the server dies after process creation but before it can
        persist the PID, another server still waits for engine.lock instead of launching a second
        engine into the same run.

        `_execute` spelled this out twice — once for the admission spawn and once for the monitor's
        re-spawn after a pre-existing engine died — so the two copies drifted on wording alone
        (doc 25 SC-07). `restarting` carries the only real difference: the operator-facing text says
        "restart" rather than "start". The record bookkeeping that legitimately differs between the
        two sites (`waiting_for_spawn`, whether a `None` pid may overwrite a known one) stays at the
        call sites where a reader can see the divergence.
        """
        verb = "restart" if restarting else "start"
        self._record_spawn_claim(rd, command_id, None)
        try:
            pid = self._spawn(rd)
        except Exception as exc:  # noqa: BLE001 - Popen/task failures become records
            uncertain = isinstance(exc, EngineSpawnOutcomeUnknown)
            if not uncertain:
                self._clear_spawn_claim(rd, command_id)
            self._terminal(path, record, "failed", error=_error(
                "engine_start_uncertain" if uncertain else "spawn_failed",
                (f"run engine {'restart' if restarting else 'creation'} crossed an uncertain "
                 "process boundary" if uncertain else
                 f"could not {verb} the run engine: {exc}"),
                ("observe the retained spawn claim; retry only after liveness or "
                 "definitive PID death clears the duplicate-start hazard"
                 if uncertain else
                 "fix the cause, then POST this command id's /retry endpoint (same intent)"),
                retryable=not uncertain))
            return True, None
        # Persist the resulting PID on the SAME lease the pre-Popen write reserved. Both call sites
        # did this identically right after their own record bookkeeping; the durable order (claim
        # before record) is unchanged, only the in-memory field assignment now follows it.
        self._record_spawn_claim(rd, command_id, pid)
        return False, pid

    def _terminalize_expired(self, rd: Path, path: Path, record: dict, command_id: str,
                             spec) -> None:
        """The monitor loop's deadline exit: one last serialized look, then a terminal write.

        Split out of `_execute` (doc 25 SC-07) — it is the only phase that runs after the loop, and
        inlining it put four more early returns and a sixth lock scope inside an already 460-line
        method. Reads the record fresh under the sequencer, so the caller's `record` is deliberately
        NOT the one written here.

        Serialize the final observation and terminal write with GET/retry. Without this last check, a
        completion arriving at the deadline could be promoted to succeeded by GET and then
        overwritten by this worker's stale timed_out write.
        """
        with self.sequence(rd):
            current = self._load(path)
            if current is not None:
                record = current
            if record.get("status") in TERMINAL_STATUSES:
                return
            final_observation = self._observe(rd)
            if self._postcondition(rd, record, final_observation):
                self._succeeded(rd, path, record)
                return
            domain_error = (self._domain_failure(rd, record, final_observation)
                            if spec.engine_policy is not EnginePolicy.NO_SPAWN else None)
            if domain_error is not None:
                self._clear_spawn_claim(rd, command_id)
                self._terminal(path, record, "failed", error=domain_error)
                return

            uncertain_start = False
            if (record.get("spawned_by_command")
                    and not record.get("spawn_claim_released")):
                if self._engine_state(rd) is True:
                    self._clear_spawn_claim(rd, command_id)
                    record["spawn_claim_released"] = True
                else:
                    uncertain_start = self._quarantine_spawn_claim(
                        rd, command_id, record.get("engine_pid"))
            else:
                self._clear_spawn_claim(rd, command_id)
            if uncertain_start:
                self._terminal(path, record, "timed_out", error=_error(
                    "engine_start_uncertain",
                    "the detached engine has not exposed engine.lock and is not known to have exited",
                    "wait and GET this command; do not retry or launch another driver while quarantined",
                    retryable=False))
            else:
                self._terminal(path, record, "timed_out", error=_error(
                    "postcondition_timeout",
                    f"command intent was recorded but {record.get('postcondition')} was not observed in time",
                    "GET may reconcile late completion; otherwise POST this command id's /retry endpoint",
                    retryable=True))

    def _admit(self, rd: Path, path: Path, record: dict, command_id: str):
        """Everything that runs under the per-run SEQUENCER, up to where the monitor loop begins.

        Split from `_execute` (doc 25 SC-07): the admission phase and the observation loop shared one
        427-line function and one `try`, so the lock scope, the spawn ladder and the deadline slide
        all had to be held in mind at once. Returns `(spec, record)` to monitor, or `(None, record)`
        when this phase already terminalized the command and there is nothing left to watch.

        The sequencer is held for the whole body and released on EVERY exit — including the early
        ones — which is what the hand-rolled `__enter__`/`__exit__` pair plus a `sequence_held` flag
        in the caller's `finally` used to do.
        """
        with self.sequence(rd):
            # Another process may have completed/rejected it while this worker waited for the run.
            record = self._load(path) or record
            if record.get("status") in TERMINAL_STATUSES:
                return None, record
            event_type = str(record.get("event_type") or "")
            spec = CONTROL_SPECS.get(event_type)
            if spec is None:
                self._terminal(path, record, "rejected", error=_error(
                    "invalid_command", f"unknown control event: {event_type!r}"))
                return None, record
            try:
                self._reject_unresolved_reset(rd, "execute this run command")
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                self._terminal(path, record, "failed", error=_error(
                    str(detail.get("code") or "run_reset_in_progress"),
                    str(detail.get("message") or "Replay owns this run namespace."),
                    str(detail.get("remediation") or (
                        "Observe the saved Replay operation before retrying this command.")),
                    retryable=exc.status_code >= 500))
                return None, record

            observation = self._observe(rd)
            intent = self._find_intent(rd, command_id, record, observation)
            decision_baseline = None
            if (record.get("attached")
                    and not self._attached_finalize_intact(rd, record, observation)):
                self._terminal(path, record, "failed", error=_error(
                    "command_intent_missing",
                    "the attached external finalize intent is missing or changed",
                    "do not retry automatically; inspect/repair the event log and command record",
                    retryable=False))
                return None, record
            recorded_event_seq = record.get("event_seq")
            if recorded_event_seq is not None and (
                    intent is None or intent.seq != recorded_event_seq):
                self._terminal(path, record, "failed", error=_error(
                    "command_intent_missing",
                    "the durable command record points to a marked intent that is missing or changed",
                    "do not retry automatically; inspect/repair the event log and command record",
                    retryable=False))
                return None, record
            # Once this command's marked intent is durable, never re-run state preflight during
            # recovery.  Its own fold may have cleared an approval gate or satisfied pause; treating
            # that changed state as a fresh submission would incorrectly turn a succeeded command
            # into rejected/noop after a server restart.
            if record.get("attached"):
                # Attachment is already a durable intent identity even though it deliberately has no
                # command marker. Never re-run fresh-state admission and accidentally append a second
                # finalize if another external event superseded it after record creation.
                decision = "already_attached"
            elif intent is None:
                gate_field = record.get("approval_gate_field")
                if gate_field in {"approval_request_seq", "spec_approval_request_seq"}:
                    admitted_gate = record.get("approval_gate_seq")
                    current_gate = getattr(self.srv.state(rd), gate_field, None)
                    if current_gate != admitted_gate:
                        self._terminal(path, record, "rejected", error=_error(
                            "approval_state_changed",
                            "the approval request changed before the intent could be recorded",
                            "refresh the run and submit a new approval command", retryable=False))
                        return None, record
                # Capture causality BEFORE folding state for the decision. In particular, an engine
                # can complete an externally-appended finalize after `_decision` observes pending but
                # before this worker continues; that run_finished must remain *after* the attach
                # baseline so it satisfies the command instead of being hidden inside the baseline.
                # Read the baseline from the incremental observation index, NOT from a full
                # `EventStore.read_all()`. `latest_seq` is the last seq of the same recoverable
                # prefix `read_all` would return — both stop at the identical torn/corrupt boundary,
                # and both report -1 for an absent or empty log — but the index only parses bytes
                # appended since the previous observation, which is exactly why it exists. Reading
                # `[-1].seq` off a fresh full parse re-read and re-validated the entire log on every
                # command request. The append below is CAS'd on this value, so it stays authoritative.
                decision_baseline = self._observe(rd).latest_seq
                decision, err = self._decision(rd, event_type)
                if (decision == "append"
                        and self._standing_hint_duplicate(
                            rd, event_type, record.get("data"))):
                    decision = "noop"
                if decision == "reject":
                    self._terminal(path, record, "rejected", error=err)
                    return None, record
                if decision == "noop":
                    self._terminal(path, record, "noop")
                    return None, record
            else:
                decision = "already_appended"
            if decision == "append" and intent is None:
                record["baseline_seq"] = decision_baseline
                event_data = dict(record.get("data") or {})
                event_data["_command_id"] = command_id
                if event_type in COLLABORATION_EVENTS:
                    intent, decision_baseline, append_error = self._append_collaboration_intent(
                        rd, record, event_data)
                    if append_error is not None:
                        status = "failed" if append_error.get("retryable") else "rejected"
                        self._terminal(path, record, status, error=append_error)
                        return None, record
                else:
                    store = EventStore(self._events_path(rd))
                if event_type in {EV_APPROVAL_GRANTED, EV_SPEC_APPROVED}:
                    # Approval is valid only against the exact decision snapshot. The per-run command
                    # sequencer does not exclude the engine/CLI, so an external grant/reset can land
                    # after `_decision`; append with CAS and re-evaluate instead of double-approving.
                    try:
                        intent = store.append(
                            event_type, event_data, expected_last_seq=decision_baseline)
                    except EventStoreConcurrencyError:
                        self._terminal(path, record, "rejected", error=_error(
                            "approval_state_changed",
                            "the approval state changed before the intent could be recorded",
                            "refresh the run and submit a new approval command", retryable=False))
                        return None, record
                elif event_type == EV_RESTART:
                    # Restart is a compound lifecycle boundary. A finalize/reset/other control that
                    # wins after admission must not be silently crossed by a later pause+resume
                    # watermark, so bind the append to the exact state snapshot just re-admitted.
                    try:
                        intent = store.append(
                            event_type, event_data, expected_last_seq=decision_baseline)
                    except EventStoreConcurrencyError:
                        self._terminal(path, record, "rejected", error=_error(
                            "restart_state_changed",
                            "the run changed before the restart intent could be recorded",
                            "refresh the run and submit a new restart command", retryable=False))
                        return None, record
                elif event_type not in COLLABORATION_EVENTS:
                    intent = store.append(event_type, event_data)
                record["baseline_seq"] = decision_baseline
            if intent is not None:
                record["event_seq"] = intent.seq
            elif "baseline_seq" not in record:
                if decision_baseline is None:
                    decision_baseline = self._observe(rd).latest_seq   # same prefix, incremental
                record["baseline_seq"] = decision_baseline
            record["status"] = "executing"
            record["updated_at"] = time.time()
            self._save(path, record)

            observation = self._observe(rd)
            if self._postcondition(rd, record, observation):
                self._succeeded(rd, path, record)
                return None, record
            domain_error = (self._domain_failure(rd, record, observation)
                            if spec.engine_policy is not EnginePolicy.NO_SPAWN else None)
            if domain_error is not None:
                self._clear_spawn_claim(rd, command_id)
                self._terminal(path, record, "failed", error=domain_error)
                return None, record

            liveness = self._engine_state(rd)
            if spec.engine_policy is not EnginePolicy.NO_SPAWN and liveness is None:
                self._terminal(
                    path, record, "failed",
                    error=self._engine_unknown_error(
                        f"start a driver for {event_type}", retryable=True))
                return None, record
            if spec.engine_policy is EnginePolicy.RESTART_AFTER_EXIT and liveness is False:
                if not self._try_restart_claim(rd, path, record):
                    return None, record
            elif spec.engine_policy is not EnginePolicy.NO_SPAWN and liveness is False:
                spawned_now = False
                if self._recent_spawn_claim(rd):
                    record["waiting_for_spawn"] = True
                    record["deadline_at"] = max(
                        float(record["deadline_at"]), time.time() + self.startup_timeout * 2 + 1)
                    self._save(path, record)
                else:
                    terminalized, pid = self._spawn_under_claim(
                        rd, path, record, command_id, restarting=False)
                    if terminalized:
                        return None, record
                    spawned_now = True
                    record["spawned_by_command"] = True
                    record["waiting_for_spawn"] = False
                    if pid is not None:
                        record["engine_pid"] = pid
                    record["updated_at"] = time.time()
                    self._save(path, record)

                if spawned_now:
                    startup_deadline = min(
                        float(record["deadline_at"]), time.time() + self.startup_timeout)
                    while time.time() < startup_deadline:
                        observation = self._observe(rd)
                        if self._postcondition(rd, record, observation):
                            if spec.engine_policy is EnginePolicy.ENSURE_RUNNING:
                                self._succeeded(rd, path, record)
                                # `(None, record)`, not a bare `return`. This method's contract is a
                                # PAIR (see the docstring), and the caller unpacks it —
                                # `spec, record = self._admit(...)`. A bare return handed it `None`,
                                # raised `TypeError: cannot unpack non-iterable NoneType`, and the
                                # caller's `except Exception` then recorded `command_worker_failed`
                                # ON TOP of the `succeeded` we had just written. So the one path that
                                # reports a command as having WORKED was the one path that reported it
                                # as failed — timing-dependent, which is why it passed in isolation.
                                return None, record
                            break
                        domain_error = self._domain_failure(rd, record, observation)
                        if domain_error is not None:
                            self._clear_spawn_claim(rd, command_id)
                            self._terminal(path, record, "failed", error=domain_error)
                            return None, record
                        # Lock is startup evidence only. ENSURE_RUNNING stays executing until the
                        # exact command_ack arrives; finalize waits for finished + dead.
                        if self._engine_state(rd) is True:
                            self._clear_spawn_claim(rd, command_id)
                            record["spawn_claim_released"] = True
                            break
                        time.sleep(self.poll_interval)
                    else:
                        # The detached PID may still be alive in a cold import before engine.lock.
                        # Keep the pre-Popen lease and let the bounded command monitor wait until its
                        # absolute deadline; never declare failure + clear the only anti-double-spawn
                        # evidence merely because the short UX startup window elapsed.
                        record["startup_slow"] = True
                        record["deadline_at"] = float(record.get(
                            "absolute_deadline_at", record["deadline_at"]))
                        record["updated_at"] = time.time()
                        self._save(path, record)

            return spec, record

    def _monitor(self, rd: Path, path: Path, record: dict, command_id: str, spec) -> None:
        """Watch for the postcondition until it arrives or the deadline expires (doc 25 SC-07).

        Runs OUTSIDE the sequencer — it re-takes it only for the moments that must be serialized (a
        replacement spawn, the terminal write). Entered only after `_admit` returned a spec, so every
        precondition it would otherwise re-check has already been established.
        """
        # Re-derived, not threaded through: it is only ever used to NAME the operation in an error
        # message, and `_admit` read it from this same record field.
        event_type = str(record.get("event_type") or "")
        observation = self._observe(rd)
        if (spec.engine_policy is EnginePolicy.ENSURE_RUNNING
                and self._postcondition(rd, record, observation)):
            self._succeeded(rd, path, record)
            return

        # The baseline is the DOMAIN cursor, matching what the slide below compares against —
        # seeding it from `latest_seq` would start the window ahead of every domain event whose
        # seq a later control append had already passed, and the first real progress would then
        # fail to slide.
        if record.get("last_progress_seq") is None:
            last_progress_seq = observation.max_non_control_seq
        else:
            last_progress_seq = int(record.get("last_progress_seq", -1))
        while True:
            self._heartbeat_execution(rd, command_id)
            observation = self._observe(rd)
            if self._postcondition(rd, record, observation):
                self._succeeded(rd, path, record)
                return
            domain_error = (self._domain_failure(rd, record, observation)
                            if spec.engine_policy is not EnginePolicy.NO_SPAWN else None)
            if domain_error is not None:
                self._clear_spawn_claim(rd, command_id)
                self._terminal(path, record, "failed", error=domain_error)
                return

            now = time.time()
            liveness = self._engine_state(rd)
            alive = liveness is True
            if (alive and record.get("spawned_by_command")
                    and not record.get("spawn_claim_released")):
                self._clear_spawn_claim(rd, command_id)
                record["spawn_claim_released"] = True
            # Pause/finalize may legitimately wait through one long evaluation or wrap-up. A
            # live lock or fresh event progress slides their observation deadline; an actually
            # stalled/dead driver still reaches a terminal timeout.
            # DOMAIN progress, not any append. Keying the slide on `latest_seq` counted CONTROL
            # and collaboration events too — and card drops/comments deliberately bypass the
            # active-driver gate, so while a finalize sat `executing` an operator who kept
            # appending them repeatedly bumped `latest_seq` and extended this observation window
            # against a stalled or dead driver that had made no finalize progress at all, bounded
            # only by `absolute_deadline_at` (~20 min). `max_non_control_seq` is the signal the
            # observation layer already builds for exactly this — it excludes CONTROL_EVENTS —
            # and the driver-liveness half of the condition (`alive`) is untouched, so a live
            # engine still slides its own deadline whether or not it has appended yet.
            progress_seq = observation.max_non_control_seq
            if ((record.get("postcondition") in {"paused_and_stopped", "finished_and_stopped"}
                 or spec.engine_policy is not EnginePolicy.NO_SPAWN)
                    and (alive or observation.has_domain_progress(last_progress_seq))):
                last_progress_seq = progress_seq
                record["last_progress_seq"] = progress_seq
                # Never shrink a longer Popen→engine.lock lease installed above. Fresh progress
                # extends a normal observation deadline, while an in-flight spawn keeps its full
                # startup window even if this is the monitor's first pass.
                record["deadline_at"] = max(
                    float(record.get("deadline_at") or 0), now + self.command_timeout)
                record["deadline_at"] = min(
                    float(record.get("absolute_deadline_at") or record["deadline_at"]),
                    float(record["deadline_at"]))
                record["updated_at"] = now
                self._save(path, record)

            if spec.engine_policy is not EnginePolicy.NO_SPAWN and liveness is None:
                # Preserve any extant spawn claim: an inaccessible lock may belong to the child
                # we launched, so terminalize observably but never clear/retry into a duplicate.
                self._terminal(
                    path, record, "failed",
                    error=self._engine_unknown_error(
                        f"continue driving {event_type}", retryable=True))
                return

            # Check the bounded absolute deadline before considering another Popen. A slow child
            # owns its lease through this boundary; expiry must terminalize the command first,
            # not launch a second child in the same monitor iteration.
            absolute_deadline = float(record.get(
                "absolute_deadline_at", record.get("deadline_at") or now))
            if now >= min(float(record["deadline_at"]), absolute_deadline):
                break

            # A pre-existing engine can die before acknowledging this intent. Re-ensure exactly
            # one driver under the same per-run sequencer; the spawn-inflight lease closes the
            # Popen→engine.lock window for other command workers/processes.
            if spec.engine_policy is EnginePolicy.RESTART_AFTER_EXIT and not alive:
                with self.sequence(rd):
                    retry_observation = self._observe(rd)
                    if self._postcondition(rd, record, retry_observation):
                        self._succeeded(rd, path, record)
                        return
                    retry_liveness = self._engine_state(rd)
                    if retry_liveness is None:
                        self._terminal(
                            path, record, "failed",
                            error=self._engine_unknown_error(
                                "start the replacement run driver", retryable=True))
                        return
                    if retry_liveness is False:
                        if not self._try_restart_claim(rd, path, record):
                            return
            elif spec.engine_policy is not EnginePolicy.NO_SPAWN and not alive:
                with self.sequence(rd):
                    retry_observation = self._observe(rd)
                    if self._postcondition(rd, record, retry_observation):
                        self._succeeded(rd, path, record)
                        return
                    retry_liveness = self._engine_state(rd)
                    if retry_liveness is None:
                        self._terminal(
                            path, record, "failed",
                            error=self._engine_unknown_error(
                                f"restart a driver for {event_type}", retryable=True))
                        return
                    if retry_liveness is False and not self._recent_spawn_claim(rd):
                        terminalized, pid = self._spawn_under_claim(
                            rd, path, record, command_id, restarting=True)
                        if terminalized:
                            return
                        record["spawned_by_command"] = True
                        record["engine_pid"] = pid
                        record["updated_at"] = time.time()
                        self._save(path, record)
                        startup_deadline = min(
                            float(record.get("absolute_deadline_at") or time.time()),
                            time.time() + self.startup_timeout)
                        while time.time() < startup_deadline:
                            self._heartbeat_execution(rd, command_id)
                            startup_observation = self._observe(rd)
                            if (self._postcondition(rd, record, startup_observation)
                                    or self._engine_state(rd) is True):
                                self._clear_spawn_claim(rd, command_id)
                                record["spawn_claim_released"] = True
                                break
                            time.sleep(self.poll_interval)

            time.sleep(self.poll_interval)
        self._terminalize_expired(rd, path, record, command_id, spec)

    def _report_worker_crash(self, rd: Path, path: Path, exc: BaseException) -> None:
        """`_execute`'s crash report: make a worker crash OBSERVABLE without re-deciding a durable
        outcome.

        `record` in the caller is the PRE-ADMISSION copy: `_admit` persists the outcome and the
        bookkeeping (`event_seq`, `baseline_seq`, the spawn lease) that `/retry` and reconciliation
        read to find the marked intent. So the report must be written against the DURABLE record and
        never that copy — a failure record that dropped `event_seq`/`baseline_seq` reads as a command
        that never appended an intent, the one state operators are told not to auto-retry. What looks
        like a single line (`current = self._load(path) or record`) is four separate decisions, and
        that spelling got each of them wrong:

        1. ABSENT vs UNREADABLE. `_load` returns `None` for both, so a record this worker merely
           could not read fell back to the pre-admission copy and the terminal check then ran against
           the wrong record — demoting a durable `succeeded` to `failed`. Not theoretical: `_save`'s
           own retry loop documents this same contention from the WRITE side (Windows denies access
           for the milliseconds another thread holds the record open for a GET, and observation
           traffic can therefore "turn an otherwise-correct command into `command_worker_failed`").
           `_read_existing` is the read-side twin that already retries it; use it.
        2. `or` vs `is not None`. `{}` is the one falsy dict, so a readable-but-EMPTY record took the
           same wrong fallback. A record we can read is the durable answer, empty or not.
        3. ABSENT means gone, not "start over". `_terminal` → `_save` re-creates missing parents, so
           writing here resurrects `.commands/cmd_*.json` inside a namespace delete/reset removed —
           and resurrects it without the bookkeeping. There is nothing to report against: return.
        4. CHECK-THEN-WRITE must be SERIALIZED. Every other terminal writer takes `self.sequence(rd)`
           — `_admit`, `_terminalize_expired` (whose docstring names exactly this hazard), `get`,
           `retry`. Unserialized, a concurrent GET verdict landing between the read and the write is
           overwritten: `command_intent_missing`/`run_generation_changed` (retryable=False) becomes
           `command_worker_failed` (retryable=True), inverting the one bit an operator acts on.

        The wait is BOUNDED because this is teardown: `_execute`'s `finally` does not release the
        `.executing` claim until this returns, and a `_terminalize_expired` that already spent the
        full budget on the same sequencer would otherwise make the worker pay it twice. Giving up is
        the safe direction — on timeout, or any other failure, the caller writes NOTHING, which leaves
        the record NONTERMINAL, and a nonterminal record is exactly what GET's crash-recovery path
        re-drives. The crash still becomes observable; it does not become a wrong answer.
        """
        with self.sequence(rd, timeout=min(self.lock_acquire_timeout,
                                           max(1.0, self.startup_timeout * 2 + 1))):
            current = self._read_existing(path)
            if current is None or current.get("status") in TERMINAL_STATUSES:
                return
            self._terminal(path, current, "failed", error=_error(
                "command_worker_failed", str(exc),
                "correct the cause, then POST this command id's /retry endpoint",
                retryable=True))

    def _execute(self, rd: Path, path: Path, initial: dict, *, claimed: bool) -> None:
        record = self._load(path) or dict(initial)
        command_id = str(record.get("id") or "")
        if record.get("absolute_deadline_at") is None:
            record["absolute_deadline_at"] = time.time() + self.max_observation_timeout
        try:
            if record.get("status") in TERMINAL_STATUSES:
                return
            spec, record = self._admit(rd, path, record, command_id)
            if spec is not None:
                self._monitor(rd, path, record, command_id, spec)
        except Exception as exc:  # noqa: BLE001 - worker failures must become observable records
            try:
                self._report_worker_crash(rd, path, exc)
            except Exception:  # noqa: BLE001 - a best-effort report; see the helper's last paragraph
                pass
        finally:
            if claimed:
                self._release_execution(rd, command_id, record_path=path)
