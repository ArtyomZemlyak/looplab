"""The general-purpose assistant: a persistent Web UI agent that can inspect the machine and runs.

Mutating permission modes additionally expose file, shell, git, knowledge, taxonomy and run-control
providers behind the configured approval policy. Plan mode remains read-only. It is the evolution of
the pre-run Genesis chat into a full assistant.

This module is the DEPENDENCY-LIGHT core: a `SessionStore` (append-only per-session transcripts under
`<run_root>/assistant/`) and a `run_turn` that assembles a toolset and drives the shared
`agent.drive_tool_loop`. The FastAPI server wires the LLM client, run-liveness probe and settings in;
keeping those injected makes the whole thing unit-testable with a scripted fake client (see
`tests/test_assistant_endpoint.py`), exactly like `genesis`/`server` are tested today.

Permission modes are honored by the tool providers rather than this module; ``run_turn`` passes the
selected mode and approval hooks down to them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable, Optional

from looplab.core.atomicio import atomic_write_text, best_effort_fsync, strict_atomic_write_text
from looplab.core.jsonutil import valid_digest_ref
from looplab.events.eventstore import iter_jsonl

# Permission modes mirror Claude Code. `plan` is the safe read-only default; mutating modes are
# enforced by the write/shell/git providers. Re-export the shared source of truth so session and
# provider mode sets cannot drift.
from looplab.tools.perm_modes import DEFAULT_MODE, MODES, normalize_mode  # noqa: F401

# The LoopLab source tree (…/<repo>/looplab/serve/assistant.py -> the repo root is parents[2]). The
# assistant may read (and, in later phases, edit) the code that runs it — this is what "fix LoopLab
# itself" needs — so the repo root is always an allowed root alongside the run-root and the user's
# home. Under a NON-editable pip install there is no repo root: parents[2] is site-packages itself,
# and handing the assistant every installed package as an always-allowed root is not what this is
# for. Detect the checkout by its pyproject and otherwise fall back to the package directory.
_PKG_ROOT = Path(__file__).resolve().parents[1]                       # …/looplab
REPO_ROOT = (_PKG_ROOT.parent if (_PKG_ROOT.parent / "pyproject.toml").is_file() else _PKG_ROOT)

_SESSION_ID_RE = re.compile(r"[0-9a-f]{16}")
_FORK_ACTION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_FORK_STAGING_RE = re.compile(r"\.fork-[0-9a-f]{16}-[0-9a-f]{16}\.tmp")
_FORK_STAGING_MAX_AGE_SECONDS = 24 * 60 * 60
_FORK_STAGING_SWEEP_INTERVAL_SECONDS = 5 * 60
_FORK_ACTIONS_DIR = ".fork-actions"
_INCOMPLETE_SESSION_TITLE = "Incomplete chat (cleanup required)"


class ForkActionConflictError(RuntimeError):
    """An idempotent fork action was reused with a different source snapshot."""


class ForkActionDeletedError(RuntimeError):
    """The exact fork action completed previously, but its child was deliberately deleted."""


class ForkActionDeletingError(RuntimeError):
    """The fork child has crossed its durable deletion barrier but still exists on disk."""


def safe_assistant_failure(exc: Exception) -> dict:
    """Return a persistable, user-facing assistant failure without provider payloads.

    Provider exceptions can embed request URLs, routed model names, account identifiers, or even
    credential fragments.  Those belong in server diagnostics, never in the chat transcript/API.
    Keep the stored contract small and allow-listed so reloads are as safe as the live error card.
    """
    raw = str(exc or "")
    status_match = re.search(r"(?:\bHTTP\s+|\bcode\D{0,6}|^\s*)(\d{3})\b", raw, re.IGNORECASE)
    status = int(status_match.group(1)) if status_match else None
    if status == 429 or re.search(r"rate[- _]?limit", raw, re.IGNORECASE):
        kind = "rate_limit"
        message = "The model provider is temporarily rate-limited. Retry shortly or choose another provider in Settings."
    elif status in {401, 403} or re.search(r"authentication|unauthori[sz]ed|credential|api[ -]?key", raw, re.IGNORECASE):
        kind = "credentials"
        message = "Assistant credentials need attention. Check the provider and API key in Settings."
    elif re.search(r"timeout|timed out|network|connection|unreachable|couldn't reach", raw, re.IGNORECASE):
        kind = "unavailable"
        message = "The assistant could not reach the model provider. Check the connection and retry."
    else:
        kind = "provider_error"
        message = "The model provider returned an error. Retry or review the provider settings."
    return {
        "error": kind,
        "error_kind": kind,
        "message": message,
        "reply": f"(assistant error: {message})",
    }


def safe_provider_failure(exc: Exception) -> dict:
    """Return the public soft-failure envelope for an owner-facing provider route.

    Keep ``error`` as a human-readable string for existing UI callers while adding the stable
    ``error_kind`` discriminator.  Both values come from the same allow-listed classifier as
    assistant transcripts; the provider exception itself is never copied into the response.
    """
    # A few owner routes wrap both provider creation and a generation-fenced activity lease in the
    # same soft-failure boundary. Preserve the one allow-listed lifecycle conflict without reflecting
    # arbitrary HTTPException detail (which can contain paths or user input) as a provider error.
    detail = getattr(exc, "detail", None)
    if (isinstance(detail, dict)
            and detail.get("code") == "run_generation_changed"):
        return {
            "error": "run_generation_changed",
            "error_kind": "run_state_conflict",
            "message": "The run was reset or replaced before this work started.",
        }
    failure = safe_assistant_failure(exc)
    return {
        "error": failure["message"],
        "error_kind": failure["error_kind"],
        "message": failure["message"],
    }


def sanitize_assistant_message(message: dict) -> dict:
    """Return a transcript message safe for API/share reads, including legacy raw failures."""
    out = dict(message or {})
    if out.get("role") != "assistant":
        return out
    kind = out.get("error_kind")
    markers = {
        "rate_limit": "429 rate-limited", "credentials": "401 authentication error",
        "unavailable": "connection timeout", "provider_error": "provider error",
    }
    content = str(out.get("content") or "")
    legacy = re.search(
        r"^\s*(?:\(?assistant error\s*:|couldn['’]t reach the model\s*\(|authenticationerror\b|\d{3}\s+client error\b|http\s+\d{3}\b)",
        content, re.IGNORECASE)
    if kind in markers:
        failure = safe_assistant_failure(RuntimeError(markers[kind]))
    elif legacy:
        failure = safe_assistant_failure(RuntimeError(content))
    else:
        return out
    out["content"] = failure["reply"]
    out["error_kind"] = failure["error_kind"]
    return out


# --------------------------------------------------------------------------- session persistence
class SessionStore:
    """Append-only assistant sessions under `<run_root>/assistant/<sid>/`.

    `meta.json` holds {id,title,created,updated,parent,mode}; `messages.jsonl` holds one turn per line
    ({role,content,ts,...}). Append is single-writer + best-effort fsync like the run chat log. The
    `assistant` dir sits beside runs but is a RESERVED id (server refuses a run named `assistant`), so
    it never collides with a real run."""

    def __init__(self, run_root):
        self.dir = Path(run_root) / "assistant"
        self._append_lock = threading.Lock()   # serialize appends so a large turn can't interleave
        # Serialize meta read-modify-write so concurrent writers (a Share click landing while a turn's
        # reply persist bumps `updated`, or two tabs switching mode) can't each read the same meta and
        # clobber the other's field — losing a `shared` flag / title / mode.
        self._meta_lock = threading.RLock()
        self._fork_receipt_lock = threading.RLock()
        self._fork_cleanup_lock = threading.Lock()
        self._last_fork_cleanup = float("-inf")
        self._maybe_cleanup_stale_fork_staging(force=True)

    def _maybe_cleanup_stale_fork_staging(self, *, force: bool = False) -> None:
        """Throttle stale-copy cleanup so a young crash directory is eventually reconsidered."""
        monotonic_now = time.monotonic()
        if (not force
                and monotonic_now - self._last_fork_cleanup
                < _FORK_STAGING_SWEEP_INTERVAL_SECONDS):
            return
        if not self._fork_cleanup_lock.acquire(blocking=False):
            return
        try:
            monotonic_now = time.monotonic()
            if (not force
                    and monotonic_now - self._last_fork_cleanup
                    < _FORK_STAGING_SWEEP_INTERVAL_SECONDS):
                return
            self._cleanup_stale_fork_staging()
            self._last_fork_cleanup = monotonic_now
        finally:
            self._fork_cleanup_lock.release()

    def _cleanup_stale_fork_staging(self, *, now: Optional[float] = None) -> None:
        """Remove only old, exact direct-child fork staging directories left by a dead process."""
        if not self.dir.exists():
            return
        timestamp = time.time() if now is None else now
        try:
            root = self.dir.resolve(strict=True)
            expected_root = self.dir.parent.resolve(strict=True) / "assistant"
            if self.dir.is_symlink() or root != expected_root:
                return
            candidates = list(self.dir.iterdir())
        except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
            return
        for candidate in candidates:
            if _FORK_STAGING_RE.fullmatch(candidate.name) is None:
                continue
            try:
                # ``is_symlink`` catches ordinary links; exact resolved identity also rejects Windows
                # junctions. Never follow either while cleaning private transcript material.
                resolved = candidate.resolve(strict=True)
                age = timestamp - candidate.stat().st_mtime
                if (candidate.is_symlink() or not candidate.is_dir()
                        or resolved != root / candidate.name
                        or not math.isfinite(age) or age < _FORK_STAGING_MAX_AGE_SECONDS):
                    continue
                shutil.rmtree(candidate)
            except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
                continue

    def _sdir(self, sid: str) -> Path:
        # Session routes are destructive (DELETE ultimately feeds this path to ``rmtree``), while
        # ``assistant/`` also contains non-session sidecars such as ``.shares`` and ``backups``.
        # Merely checking "direct child" therefore lets a crafted sid erase those stores.  Keep the
        # generated 16-hex namespace authoritative at this single path boundary and reject a symlink
        # (or a junction resolving to a differently named sibling) before any caller touches disk.
        if not isinstance(sid, str) or _SESSION_ID_RE.fullmatch(sid) is None:
            raise ValueError("bad session id")
        candidate = self.dir / sid
        try:
            if candidate.is_symlink():
                raise ValueError("bad session id")
            root = self.dir.resolve()
            d = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError("bad session id") from exc
        if d.parent != root or d.name != sid:
            raise ValueError("bad session id")
        return d

    def _meta_path(self, sid: str) -> Path:
        return self._sdir(sid) / "meta.json"

    def _msgs_path(self, sid: str) -> Path:
        return self._sdir(sid) / "messages.jsonl"

    def mutation_journal_path(self, sid: str, turn_id: str) -> Path:
        """Private durable mutation journal path for one server-issued assistant turn id."""
        digest = hashlib.sha256(str(turn_id).encode("utf-8")).hexdigest()
        return self._sdir(sid) / "turn_mutations" / f"{digest}.json"

    @staticmethod
    def valid_fork_action_id(action_id) -> bool:
        return isinstance(action_id, str) and _FORK_ACTION_ID_RE.fullmatch(action_id) is not None

    @staticmethod
    def _valid_expected_messages(value) -> bool:
        return not isinstance(value, bool) and isinstance(value, int) and value >= 0

    def _fork_child_id(self, sid: str, action_id: str) -> str:
        self._sdir(sid)
        if not self.valid_fork_action_id(action_id):
            raise ValueError("bad fork action id")
        # The action identity deterministically owns one child path, so a lost-response retry can find
        # the original result instead of minting a second session.
        return hashlib.sha256(f"{sid}:{action_id}".encode("ascii")).hexdigest()[:16]

    def _fork_actions_dir(self, *, create: bool = False) -> Optional[Path]:
        """Return the hidden durable action ledger without following links or junctions."""
        try:
            self.dir.lstat()
        except FileNotFoundError:
            return None
        try:
            root = self.dir.resolve(strict=True)
            expected_root = self.dir.parent.resolve(strict=True) / "assistant"
            if self.dir.is_symlink() or root != expected_root:
                raise OSError("Assistant fork ledger root is not safe")
            directory = self.dir / _FORK_ACTIONS_DIR
            try:
                directory.lstat()
            except FileNotFoundError:
                # The strict record writer creates and durably publishes this one missing parent.
                return directory if create else None
            if (directory.is_symlink() or not directory.is_dir()
                    or directory.resolve(strict=True) != root / _FORK_ACTIONS_DIR):
                raise OSError("Assistant fork ledger is not a safe directory")
            return directory
        except (FileNotFoundError, NotADirectoryError, RuntimeError) as exc:
            raise OSError("Assistant fork ledger is unavailable") from exc

    def _fork_action_path(self, child_id: str, *, create_dir: bool = False) -> Optional[Path]:
        self._sdir(child_id)
        directory = self._fork_actions_dir(create=create_dir)
        return None if directory is None else directory / f"{child_id}.json"

    def _validate_fork_receipt(self, receipt, child_id: str, *, ledger: bool) -> dict:
        keys = {"source", "action_id", "child", "expected_messages"}
        if ledger:
            keys.add("status")
        if not isinstance(receipt, dict) or set(receipt) != keys:
            raise OSError("Assistant fork receipt has an invalid shape")
        source = receipt.get("source")
        action_id = receipt.get("action_id")
        if (receipt.get("child") != child_id
                or not self._valid_expected_messages(receipt.get("expected_messages"))):
            raise OSError("Assistant fork receipt has invalid fields")
        try:
            owned_child = self._fork_child_id(source, action_id)
        except ValueError as exc:
            raise OSError("Assistant fork receipt has invalid identity") from exc
        if owned_child != child_id:
            raise OSError("Assistant fork receipt does not own its child")
        if ledger and receipt.get("status") not in {"prepared", "deleted"}:
            raise OSError("Assistant fork ledger has an invalid state")
        return receipt

    def _read_fork_action(self, child_id: str) -> Optional[dict]:
        path = self._fork_action_path(child_id)
        if path is None:
            return None
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        try:
            directory = path.parent.resolve(strict=True)
            if (path.is_symlink() or not path.is_file()
                    or path.resolve(strict=True) != directory / path.name
                    or path.stat().st_size > 4096):
                raise OSError("Assistant fork ledger record is not a safe file")
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, UnicodeError, ValueError,
                RuntimeError) as exc:
            raise OSError("Assistant fork ledger record is unreadable") from exc
        return self._validate_fork_receipt(receipt, child_id, ledger=True)

    def _write_fork_action(self, receipt: dict) -> dict:
        child_id = receipt.get("child") if isinstance(receipt, dict) else ""
        validated = self._validate_fork_receipt(receipt, child_id, ledger=True)
        with self._fork_receipt_lock:
            path = self._fork_action_path(child_id, create_dir=True)
            if path is None:
                raise OSError("Assistant fork ledger is unavailable")
            try:
                if path.is_symlink():
                    raise OSError("Assistant fork ledger record is not a safe file")
                # Deletion may only cross rmtree after this file and its directory entry received a
                # strict durability receipt. A failed strict write is never treated as confirmation;
                # prepare retries the same record on the next DELETE.
                strict_atomic_write_text(path, json.dumps(validated, sort_keys=True))
                written = self._read_fork_action(child_id)
            except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as exc:
                raise OSError("Assistant fork ledger record could not be stored") from exc
            if written != validated:
                raise OSError("Assistant fork ledger record could not be verified")
            return written

    def _published_fork(self, child_id: str, *,
                        allow_unmarked: bool = False) -> Optional[tuple[dict, dict]]:
        """Return a child's strict private receipt and metadata, or None when it is absent."""
        directory = self._sdir(child_id)
        try:
            directory.lstat()
        except FileNotFoundError:
            return None
        try:
            root = self.dir.resolve(strict=True)
            if (directory.is_symlink() or not directory.is_dir()
                    or directory.resolve(strict=True) != root / child_id):
                raise OSError("Assistant fork action path is not a safe directory")
            marker = directory / ".fork.json"
            try:
                marker.lstat()
            except FileNotFoundError:
                if allow_unmarked:
                    return None
                raise OSError("Assistant fork receipt is missing")
            if (marker.is_symlink() or not marker.is_file()
                    or marker.resolve(strict=True) != directory / ".fork.json"
                    or marker.stat().st_size > 4096):
                raise OSError("Assistant fork receipt is not a safe file")
            receipt = json.loads(marker.read_text(encoding="utf-8"))
            receipt = self._validate_fork_receipt(receipt, child_id, ledger=False)
            meta = self._read_meta(child_id)
            if (not self._valid_meta(child_id, meta)
                    or meta.get("parent") != receipt["source"]):
                raise OSError("Assistant fork child metadata is invalid")
            return receipt, meta
        except (FileNotFoundError, NotADirectoryError, UnicodeError, ValueError,
                RuntimeError) as exc:
            raise OSError("Assistant fork receipt is unreadable") from exc

    @staticmethod
    def _fork_identity_matches(receipt: dict, sid: str, action_id: str,
                               expected_messages: Optional[int]) -> None:
        if receipt.get("source") != sid or receipt.get("action_id") != action_id:
            raise OSError("Assistant fork action identity is invalid")
        if (expected_messages is not None
                and receipt.get("expected_messages") != expected_messages):
            raise ForkActionConflictError(
                "Fork action belongs to a different source transcript version")

    def fork_result(self, sid: str, action_id: str, *,
                    expected_messages: Optional[int] = None) -> Optional[dict]:
        """Return the durable result for one exact action, including deleted terminal state."""
        if expected_messages is not None and not self._valid_expected_messages(expected_messages):
            raise ValueError("bad fork source version")
        child_id = self._fork_child_id(sid, action_id)
        ledger = self._read_fork_action(child_id)
        if ledger is not None:
            self._fork_identity_matches(ledger, sid, action_id, expected_messages)
            if ledger["status"] == "deleted":
                raise ForkActionDeletedError("Fork child was deleted")
            published = self._published_fork(child_id)
            if published is None:
                raise ForkActionDeletedError("Fork child was deleted")
            marker, _meta = published
            if marker != {key: value for key, value in ledger.items() if key != "status"}:
                raise OSError("Assistant fork ledger does not match its child")
            # Never acknowledge a child after deletion crossed its durable barrier: it can vanish
            # immediately after the response and make the browser discard exact recovery state.
            raise ForkActionDeletingError("Fork child is being deleted")

        published = self._published_fork(child_id)
        if published is None:
            return None
        marker, meta = published
        self._fork_identity_matches(marker, sid, action_id, expected_messages)
        return meta

    def prepare_fork_deletion(self, child_id: str) -> Optional[dict]:
        """Persist a terminal barrier before deleting a child created by an idempotent fork."""
        self._sdir(child_id)
        with self._fork_receipt_lock:
            existing = self._read_fork_action(child_id)
            if existing is not None:
                if existing["status"] == "deleted":
                    return existing
                # Rewrite even an already-visible `prepared` record. A previous strict write can fail
                # after replacement became visible; only this successful retry authorizes rmtree.
                return self._write_fork_action({**existing, "status": "prepared"})
            published = self._published_fork(child_id, allow_unmarked=True)
            if published is None:
                return None
            marker, _meta = published
            return self._write_fork_action({**marker, "status": "prepared"})

    def finish_fork_deletion(self, receipt: dict) -> None:
        """Mark a prepared deletion complete; prepared+absent already fails closed if this write fails."""
        child_id = receipt.get("child") if isinstance(receipt, dict) else ""
        expected = self._validate_fork_receipt(receipt, child_id, ledger=True)
        with self._fork_receipt_lock:
            current = self._read_fork_action(child_id)
            if current is None or any(current.get(key) != expected.get(key) for key in (
                    "source", "action_id", "child", "expected_messages")):
                raise OSError("Assistant fork deletion receipt changed")
            if current["status"] != "deleted":
                self._write_fork_action({**current, "status": "deleted"})

    def create(self, title: str = "", parent: Optional[str] = None, mode: str = DEFAULT_MODE,
               *, now: Optional[float] = None) -> dict:
        sid = secrets.token_hex(8)
        d = self._sdir(sid)
        d.mkdir(parents=True, exist_ok=True)
        ts = time.time() if now is None else now
        meta = {"id": sid, "title": (title or "New chat")[:120], "created": ts, "updated": ts,
                "parent": parent, "mode": normalize_mode(mode)}
        atomic_write_text(self._meta_path(sid), json.dumps(meta))
        return meta

    def _read_meta(self, sid: str) -> Optional[dict]:
        try:
            meta = json.loads(self._meta_path(sid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    @staticmethod
    def _valid_timestamp(value) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return value >= 0 and math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            return False

    @classmethod
    def _valid_meta(cls, sid: str, meta: Optional[dict]) -> bool:
        if meta is None or meta.get("id") != sid or not isinstance(meta.get("title"), str):
            return False
        if not cls._valid_timestamp(meta.get("created")) or not cls._valid_timestamp(meta.get("updated")):
            return False
        parent = meta.get("parent")
        if parent is not None and (not isinstance(parent, str) or _SESSION_ID_RE.fullmatch(parent) is None):
            return False
        return meta.get("mode") in MODES

    @staticmethod
    def _cleanup_meta(sid: str, directory: Path) -> dict:
        try:
            timestamp = float(directory.stat().st_mtime)
        except (OSError, OverflowError, TypeError, ValueError):
            timestamp = 0.0
        if timestamp < 0 or not math.isfinite(timestamp):
            timestamp = 0.0
        return {
            "id": sid,
            "title": _INCOMPLETE_SESSION_TITLE,
            "created": timestamp,
            "updated": timestamp,
            "parent": None,
            "mode": DEFAULT_MODE,
            "cleanup_required": True,
        }

    def update_meta(self, sid: str, **fields) -> Optional[dict]:
        # Read-modify-write under the meta lock so concurrent updates don't drop each other's fields.
        with self._meta_lock:
            meta = self._read_meta(sid)
            if meta is None:
                return None
            meta.update({k: v for k, v in fields.items() if v is not None})
            meta["updated"] = fields.get("updated", time.time())
            atomic_write_text(self._meta_path(sid), json.dumps(meta))
            return meta

    def list(self) -> list[dict]:
        self._maybe_cleanup_stale_fork_staging()
        if not self.dir.exists():
            return []
        out = []
        root = self.dir.resolve()
        for d in self.dir.iterdir():
            try:
                if (d.is_symlink() or not d.is_dir() or d.resolve().parent != root
                        or _SESSION_ID_RE.fullmatch(d.name) is None):
                    continue
            except (OSError, RuntimeError):
                continue
            meta = self._read_meta(d.name)
            out.append(meta if self._valid_meta(d.name, meta) else self._cleanup_meta(d.name, d))
        out.sort(key=lambda m: m.get("updated", 0), reverse=True)
        return out

    def messages(self, sid: str) -> list[dict]:
        try:
            # Canonicalize legacy assistant failures at the storage boundary. This keeps old raw
            # provider URLs/account metadata out of owner/shared reads, future model prompts, and
            # forked transcripts while leaving user-authored messages untouched.
            return [sanitize_assistant_message(message)
                    for message in iter_jsonl(self._msgs_path(sid))]
        except OSError:
            return []

    def fork_source_snapshot(self, sid: str) -> Optional[dict]:
        """Read a fork source strictly so corruption cannot publish a silent transcript prefix."""
        directory = self._sdir(sid)
        try:
            directory.lstat()
        except FileNotFoundError:
            return None
        try:
            root = self.dir.resolve(strict=True)
            if (directory.is_symlink() or not directory.is_dir()
                    or directory.resolve(strict=True) != root / sid):
                raise OSError("Assistant fork source is not a safe directory")
            meta = self._read_meta(sid)
            if not self._valid_meta(sid, meta):
                raise OSError("Assistant fork source metadata is invalid")
            path = directory / "messages.jsonl"
            if path.is_symlink():
                raise OSError("Assistant fork source transcript is not a safe file")
            try:
                path.lstat()
            except FileNotFoundError:
                return {"meta": meta, "messages": []}
            if (not path.is_file()
                    or path.resolve(strict=True) != directory / "messages.jsonl"):
                raise OSError("Assistant fork source transcript is not a safe file")
        except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as exc:
            raise OSError("Assistant fork source is unavailable") from exc

        messages: list[dict] = []
        try:
            # Serialize with the in-process append writer. The router's per-source lifecycle fence
            # prevents a new turn claim; this lock also closes a late persistence read boundary.
            with self._append_lock:
                with open(path, "rb") as stream:
                    for raw in stream:
                        if not raw.endswith(b"\n"):
                            raise OSError("Assistant fork source has a torn transcript tail")
                        try:
                            message = json.loads(raw)
                        except (UnicodeError, ValueError, RecursionError) as exc:
                            raise OSError("Assistant fork source transcript is corrupt") from exc
                        if not isinstance(message, dict):
                            raise OSError("Assistant fork source transcript contains a non-object row")
                        messages.append(sanitize_assistant_message(message))
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise OSError("Assistant fork source transcript disappeared") from exc
        return {"meta": meta, "messages": messages}

    def validated_meta(self, sid: str) -> Optional[dict]:
        """Return authoritative session metadata without materializing its transcript."""
        meta = self._read_meta(sid)
        return meta if self._valid_meta(sid, meta) else None

    def bounded_complete_messages(
            self, sid: str, *, upto: Optional[int], max_messages: int,
            max_line_bytes: int, max_total_bytes: int,
            report_incomplete: bool = False,
            ) -> Optional[tuple[list[dict], bool] | tuple[list[dict], bool, bool]]:
        """Read a bounded, complete public transcript prefix without blocking owner appends.

        Public share limits must apply *before* JSON materialization: applying them after
        :meth:`messages` lets one oversized/hidden JSONL field consume unbounded memory on every
        anonymous request.  The append-only writer always emits one complete JSON object plus a
        newline, so a lock-free binary reader can safely stop at a torn concurrent tail.  The boolean
        reports that otherwise-public data was omitted; a lone live ``user`` at clean EOF is merely
        an in-progress turn and is not public yet.  Minting can request a third boolean that reports
        such an incomplete pair without changing the live public reader's truncation semantics.
        """
        if (isinstance(max_messages, bool) or not isinstance(max_messages, int)
                or max_messages < 0 or max_messages % 2
                or isinstance(max_line_bytes, bool) or not isinstance(max_line_bytes, int)
                or max_line_bytes <= 0
                or isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int)
                or max_total_bytes <= 0
                or (upto is not None and (
                    isinstance(upto, bool) or not isinstance(upto, int) or upto < 0 or upto % 2))):
            raise ValueError("invalid bounded transcript limits")

        try:
            directory = self._sdir(sid)
            if not directory.is_dir():
                return None
            path = directory / "messages.jsonl"
            if path.is_symlink():
                return None
            if not path.exists():
                # A newly-created, empty session legitimately has no transcript file yet.
                empty = ([], False) if upto in (None, 0) else ([], True)
                return (*empty, False) if report_incomplete else empty
            resolved = path.resolve(strict=True)
            if (resolved.parent != directory or resolved.name != "messages.jsonl"
                    or not resolved.is_file()):
                return None
        except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError, ValueError):
            return None

        frozen = upto is not None
        target = min(upto, max_messages) if frozen else max_messages + 2
        truncated = bool(frozen and upto > max_messages)
        complete: list[dict] = []
        pending_user: Optional[dict] = None
        decoded = 0
        consumed = 0

        try:
            with open(resolved, "rb") as stream:
                while decoded < target:
                    remaining = max_total_bytes - consumed
                    if remaining <= 0:
                        truncated = True
                        break
                    # The extra byte is a sentinel only: it detects either a physical-row cap or the
                    # aggregate cap without ever materializing the rest of an attacker-sized line.
                    raw = stream.readline(min(max_line_bytes, remaining) + 1)
                    if not raw:
                        if frozen and decoded < target:
                            truncated = True
                        break
                    if len(raw) > remaining or len(raw) > max_line_bytes:
                        truncated = True
                        break
                    consumed += len(raw)
                    if not raw.endswith(b"\n"):
                        truncated = True
                        break
                    try:
                        message = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        truncated = True
                        break
                    if not isinstance(message, dict):
                        truncated = True
                        break
                    expected_role = "user" if pending_user is None else "assistant"
                    if message.get("role") != expected_role:
                        truncated = True
                        break
                    decoded += 1
                    if pending_user is None:
                        pending_user = message
                        continue
                    # A full pair beyond the public message budget is lookahead evidence only.
                    if len(complete) >= max_messages:
                        pending_user = None
                        truncated = True
                        break
                    complete.extend((pending_user, message))
                    pending_user = None
        except (FileNotFoundError, NotADirectoryError, OSError):
            return None
        if report_incomplete:
            return complete, truncated, pending_user is not None
        return complete, truncated

    def get(self, sid: str) -> Optional[dict]:
        meta = self._read_meta(sid)
        if meta is None:
            return None
        return {"meta": meta, "messages": self.messages(sid)}

    def append(self, sid: str, turn: dict) -> None:
        d = self._sdir(sid)
        if not d.exists():
            raise ValueError("no such session")
        line = {**turn, "ts": turn.get("ts", time.time())}
        # A large turn (attached-file contents) exceeds the buffer and becomes multiple write() syscalls
        # that can interleave with a concurrent append → a corrupt mid-file line, which iter_jsonl stops
        # at, silently dropping every later turn on the next read. Serialize appends to prevent that.
        with self._append_lock:
            with open(self._msgs_path(sid), "ab") as f:
                f.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
                try:
                    best_effort_fsync(f.fileno())
                except OSError:
                    pass
        self.update_meta(sid, updated=line["ts"])

    def append_if_len(self, sid: str, turn: dict, expected_len: int) -> bool:
        """Append `turn` ONLY if the transcript currently holds exactly `expected_len` messages —
        the check and the write happen atomically under the append lock. Returns True if appended,
        False if a concurrent turn changed the length in between (so a late or cancelled reply can't
        interleave into a newer turn's transcript, e.g. u1,u2,a1,a2). Closes the TOCTOU window a
        separate 'count then append' left open."""
        d = self._sdir(sid)
        if not d.exists():
            return False
        line = {**turn, "ts": turn.get("ts", time.time())}
        with self._append_lock:
            try:
                cur = sum(1 for _ in iter_jsonl(self._msgs_path(sid)))
            except OSError:
                cur = -1
            if cur != expected_len:
                return False
            with open(self._msgs_path(sid), "ab") as f:
                f.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
                try:
                    best_effort_fsync(f.fileno())
                except OSError:
                    pass
        self.update_meta(sid, updated=line["ts"])
        return True

    def fork_snapshot(self, sid: str, src: dict, *, action_id: Optional[str] = None,
                      expected_messages: Optional[int] = None, publish_guard=None,
                      now: Optional[float] = None) -> dict:
        """Atomically publish a caller-fenced source snapshot as a fresh child session.

        Building directly in the final 16-hex directory exposes a half-copied chat to list/get/send;
        copying through ``append`` also performs one fsync and meta rewrite per message. A hidden
        same-parent directory is outside the session namespace, and one final rename publishes the
        complete transcript + metadata at once.
        """
        self._maybe_cleanup_stale_fork_staging()
        # Validate the parent at the store boundary even though the router already fetched it. The
        # caller supplies the immutable snapshot so a per-session fence can avoid a second source read.
        self._sdir(sid)
        if (not isinstance(src, dict) or not isinstance(src.get("meta"), dict)
                or not isinstance(src.get("messages"), list)):
            raise ValueError("invalid session snapshot")
        source_meta = src["meta"]
        source_title = source_meta.get("title", "chat")
        if source_meta.get("id") != sid or not isinstance(source_title, str):
            raise ValueError("invalid session snapshot")
        source_mode = normalize_mode(source_meta.get("mode", DEFAULT_MODE))
        messages = src["messages"]
        snapshot_messages = len(messages)
        if expected_messages is None:
            expected_messages = snapshot_messages
        if (not self._valid_expected_messages(expected_messages)
                or expected_messages != snapshot_messages):
            raise ForkActionConflictError("Fork source transcript version changed")
        forked_at = time.time() if now is None else now
        root = self.dir.resolve()
        child_id = ""
        child_dir = None
        if action_id is not None:
            existing = self.fork_result(
                sid, action_id, expected_messages=expected_messages)
            if existing is not None:
                return existing
            child_id = self._fork_child_id(sid, action_id)
            child_dir = self._sdir(child_id)
            if child_dir.exists() or child_dir.is_symlink():
                raise OSError("Assistant fork action path is occupied")
        else:
            for _ in range(32):
                candidate_id = secrets.token_hex(8)
                candidate = self._sdir(candidate_id)
                try:
                    available = not candidate.exists() and not candidate.is_symlink()
                except OSError:
                    available = False
                if available:
                    child_id, child_dir = candidate_id, candidate
                    break
            if child_dir is None:
                raise OSError("could not reserve a unique Assistant session id")

        temp_dir = None
        for _ in range(32):
            candidate = self.dir / f".fork-{child_id}-{secrets.token_hex(8)}.tmp"
            try:
                if candidate.resolve(strict=False).parent != root:
                    continue
                candidate.mkdir(parents=False, exist_ok=False)
                temp_dir = candidate
                break
            except FileExistsError:
                continue
        if temp_dir is None:
            raise OSError("could not reserve temporary Assistant fork storage")

        child = {
            "id": child_id,
            "title": ("Fork of " + source_title)[:120],
            "created": forked_at,
            "updated": forked_at,
            "parent": sid,
            "mode": source_mode,
        }
        published = False
        fork_receipt = None
        try:
            if messages:
                with open(temp_dir / "messages.jsonl", "xb") as stream:
                    for turn in messages:
                        if not isinstance(turn, dict):
                            raise ValueError("invalid session snapshot")
                        line = {**turn, "ts": turn.get("ts", forked_at)}
                        stream.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
                    try:
                        best_effort_fsync(stream.fileno())
                    except OSError:
                        pass
            atomic_write_text(temp_dir / "meta.json", json.dumps(child))
            if action_id is not None:
                fork_receipt = {
                    "source": sid, "action_id": action_id, "child": child_id,
                    "expected_messages": expected_messages,
                }
                atomic_write_text(
                    temp_dir / ".fork.json", json.dumps(fork_receipt, sort_keys=True))
            # The copy itself stays outside the lifecycle lock. Only the final recheck and rename are
            # serialized with deletion, preventing a published child from crossing its delete barrier.
            guard = publish_guard if publish_guard is not None else nullcontext()
            with guard:
                if (self.dir.resolve() != root or temp_dir.resolve().parent != root
                        or child_dir.resolve(strict=False).parent != root):
                    raise OSError("Assistant fork storage changed before publication")
                if action_id is not None:
                    existing = self.fork_result(
                        sid, action_id, expected_messages=expected_messages)
                    if existing is not None:
                        return existing
                if child_dir.exists() or child_dir.is_symlink():
                    raise OSError("Assistant fork storage changed before publication")
                try:
                    os.replace(temp_dir, child_dir)
                except OSError:
                    # A deterministic retry can observe a result published between the path check and
                    # rename. Any other occupant fails closed instead of being overwritten.
                    existing = (self.fork_result(
                        sid, action_id, expected_messages=expected_messages)
                        if action_id is not None else None)
                    if existing is not None:
                        return existing
                    raise
                published = True
                return child
        finally:
            if not published:
                # Only this unguessable, direct-child staging path is ever removed. A failed fork is
                # therefore invisible and retryable instead of leaving a public partial child behind.
                try:
                    if (_FORK_STAGING_RE.fullmatch(temp_dir.name) is not None
                            and not temp_dir.is_symlink()
                            and temp_dir.resolve(strict=True) == root / temp_dir.name):
                        shutil.rmtree(temp_dir, ignore_errors=True)
                except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
                    pass

    def fork(self, sid: str, *, now: Optional[float] = None) -> Optional[dict]:
        """Clone a session's transcript into a fresh child session (OpenCode-style fork)."""
        src = self.fork_source_snapshot(sid)
        if src is None:
            return None
        return self.fork_snapshot(sid, src, now=now)


# --------------------------------------------------------------------------- share capabilities
# Sharing a chat used to mean flipping `shared: true` on the session and handing out its OWN id. That
# made the session id a public capability: anyone with the URL could read the chat forever, saw every
# turn the owner added AFTERWARDS, and the only way to take it back was deleting the chat. A share is
# a capability, so it gets what capabilities get — its own high-entropy secret (stored only as a
# digest, like the review links in `serve/reviews.py`), an expiry, a revoke, and an explicit answer to
# "does this follow the conversation or freeze it?".
SHARE_DEFAULT_TTL_SECONDS = 7 * 24 * 3600
SHARE_MIN_TTL_SECONDS = 60
SHARE_MAX_TTL_SECONDS = 90 * 24 * 3600
SHARE_MAX_RECORDS = 4096
SHARE_MAX_MESSAGE_COUNT = 1_000_000
SHARE_TITLE_MAX_CHARS = 120
# Revoked tombstones remain long enough for ordinary stale clients to keep seeing a dead capability.
# After that, removing one is safe: a freshly minted record uses a random id AND a fresh secret hash,
# so an old token still cannot authenticate even in the vanishingly unlikely event of id reuse.
SHARE_REVOKED_RETENTION_SECONDS = SHARE_MAX_TTL_SECONDS


# One process lock PER STORE PATH, shared by every ShareStore instance in this interpreter, so two
# server objects over the same `.shares` dir cannot each hold their own private lock (doc 25 SC-10).
_SHARE_STORE_LOCKS_GUARD = threading.Lock()
_SHARE_STORE_LOCKS: dict[str, threading.Lock] = {}
_SHARE_STORE_LOCK_TIMEOUT_SECONDS = 5.0


class ShareError(ValueError):
    """A share capability request failed before a token could be published."""

    def __init__(self, message: str, *, code: str = "assistant_share_invalid",
                 status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ShareStore:
    """One-file-per-capability share links under `<run_root>/assistant/.shares/`.

    A record is `{id, session, token_hash, created_at, expires_at, revoked_at, live, upto, title}`. The
    token itself is never stored: `resolve` hashes the presented one and compares, so a leaked store
    cannot be replayed as a link. `.shares` sits under the assistant dir, which the server already
    reserves as a non-run id, and its name starts with a dot so it can never collide with a session
    id (`create` mints those from `token_hex`)."""

    def __init__(self, run_root):
        self.root = Path(run_root) / "assistant"
        self.dir = self.root / ".shares"
        self._lock = self._process_lock()
        self._lock_path = self.dir / ".lock"

    def _process_lock(self) -> threading.Lock:
        # Keyed on the STORE PATH, not the instance: `abspath` is lexical, so a missing `.shares`
        # dir cannot turn construction into an I/O failure.
        key = os.path.normcase(os.path.abspath(os.fspath(Path(self.root) / ".shares")))
        with _SHARE_STORE_LOCKS_GUARD:
            return _SHARE_STORE_LOCKS.setdefault(key, threading.Lock())

    @contextmanager
    def _store_lock(self):
        """Serialize MUTATIONS across server workers, not just threads (doc 25 SC-10).

        A share link is a bearer capability; `revoke_session` is a read-modify-write over a directory
        of records, and with only an in-process lock two uvicorn workers could interleave it — one
        worker's revocation lost behind the other's write, leaving a link the owner believes is dead.
        `ReviewStore` already pairs a per-path process lock with a REQUIRED OS lock for exactly this,
        and this is now the same pattern rather than a second, weaker one.

        Non-blocking on purpose: a contended store gives the HTTP layer a bounded, retryable 503
        instead of parking a request thread behind another worker indefinitely. There is deliberately
        no thread-only fallback — a filesystem that cannot provide the ordering fails closed.
        """
        from looplab.events.eventstore import (
            EventStoreLockError, InterprocessLockContended, _interprocess_lock)

        if not self._lock.acquire(timeout=_SHARE_STORE_LOCK_TIMEOUT_SECONDS):
            raise self._store_unavailable()
        try:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                with _interprocess_lock(self._lock_path, required=True, blocking=False):
                    yield
            except (EventStoreLockError, InterprocessLockContended, OSError) as exc:
                raise self._store_unavailable() from exc
        finally:
            self._lock.release()

    def _safe_dir_locked(self, *, create: bool = False) -> Optional[Path]:
        """Return the real, direct ``.shares`` directory or fail closed.

        A directory symlink/junction is especially dangerous here: globbing it would enumerate an
        unrelated tree and pruning could then delete arbitrary ``*.json`` files from that target.
        Resolve the assistant root first and require the share store to remain its literal direct
        child.  Normal pre-existing stores (including legacy records) keep working unchanged.
        """
        try:
            if create:
                self.root.mkdir(parents=True, exist_ok=True)
            elif not self.root.is_dir():
                return None
            root = self.root.resolve(strict=True)
            if not root.is_dir():
                return None

            if create:
                try:
                    self.dir.mkdir(exist_ok=False)
                except FileExistsError:
                    pass
            elif not self.dir.exists():
                return None

            # Reject ordinary symlinks explicitly.  On Windows, directory junctions may not report
            # as symlinks on every supported Python version, so the resolved-path equality is the
            # authoritative check for both forms of redirection.
            if self.dir.is_symlink():
                return None
            resolved = self.dir.resolve(strict=True)
            expected = root / ".shares"
            if resolved != expected or not resolved.is_dir():
                return None
            return resolved
        except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
            return None

    def _store_entry_exists_locked(self) -> bool:
        """Check the directory entry without following a symlink/junction target."""
        try:
            self.dir.lstat()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            # An unreadable entry is not safely equivalent to an absent store.
            return True

    @staticmethod
    def _store_unavailable() -> ShareError:
        return ShareError(
            "Assistant share storage is unavailable",
            code="assistant_share_store_unavailable",
            status_code=503,
        )

    @staticmethod
    def _now(value: Optional[float] = None) -> Optional[float]:
        # Stored authorization timestamps are deliberately stricter than ``float(value)``: booleans
        # and numeric-looking strings are not timestamps.  Accepting them would make a hand-edited or
        # partially corrupted record silently regain authority under Python's coercion rules.
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return None
        try:
            current = time.time() if value is None else float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return current if current >= 0 and math.isfinite(current) else None

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _path(self, link_id: str, directory: Path) -> Optional[Path]:
        # Validate the SHAPE before touching the filesystem: the id arrives inside an attacker-chosen
        # URL, and only a fixed-width hex string may become a pathname here.
        if not (len(link_id) == 32 and all(c in "0123456789abcdef" for c in link_id)):
            return None
        return directory / f"{link_id}.json"

    def _safe_record_path_locked(self, path: Path, *, strict_io: bool = False) -> Optional[Path]:
        """Return a regular record path that is still inside the verified share directory."""
        directory = self._safe_dir_locked()
        if directory is None:
            if strict_io:
                raise self._store_unavailable()
            return None
        try:
            if path.parent != directory or path.is_symlink():
                if strict_io:
                    raise self._store_unavailable()
                return None
            resolved = path.resolve(strict=True)
            if resolved.parent != directory or resolved.name != path.name or not resolved.is_file():
                if strict_io:
                    raise self._store_unavailable()
                return None
            return resolved
        except ShareError:
            raise
        except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError) as exc:
            if strict_io:
                raise self._store_unavailable() from exc
            return None

    def _read(self, path: Path, *, strict_io: bool = False) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            if strict_io:
                raise self._store_unavailable() from exc
            return None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _validated_record(self, path: Path, record: Optional[dict] = None,
                          *, strict_io: bool = False) -> Optional[dict]:
        """Return one complete, fail-closed capability record.

        These files are durable authorization state.  A NaN expiry, mismatched filename/id, malformed
        snapshot bound, or non-boolean ``live`` must revoke access rather than silently becoming an
        immortal/broader capability.  Extra keys are ignored by the exact public projection below.
        """
        path = self._safe_record_path_locked(path, strict_io=strict_io)
        if path is None:
            return None
        record = self._read(path, strict_io=strict_io) if record is None else record
        if not isinstance(record, dict):
            return None
        link_id = record.get("id")
        session = record.get("session")
        token_hash = record.get("token_hash")
        # Records minted before share titles were introduced are still safe to honor, but their mutable
        # session meta must never be consulted.  Give that one legacy shape a fixed public title;
        # malformed explicit titles remain fail-closed.
        title = "Shared chat" if "title" not in record else record.get("title")
        if (not isinstance(link_id, str) or path.stem != link_id
                or len(link_id) != 32 or any(c not in "0123456789abcdef" for c in link_id)
                or not isinstance(session, str) or _SESSION_ID_RE.fullmatch(session) is None
                or not valid_digest_ref(token_hash)
                or not isinstance(title, str) or not title.strip()
                or len(title) > SHARE_TITLE_MAX_CHARS):
            return None
        # ``_now(None)`` intentionally means wall-clock time for callers; a durable record must not
        # inherit that convenience when a required timestamp is absent/null.
        if record.get("created_at") is None or record.get("expires_at") is None:
            return None
        created = self._now(record.get("created_at"))
        expires = self._now(record.get("expires_at"))
        if created is None or expires is None:
            return None
        lifetime = expires - created
        if not SHARE_MIN_TTL_SECONDS <= lifetime <= SHARE_MAX_TTL_SECONDS:
            return None
        revoked_raw = record.get("revoked_at")
        revoked = None if revoked_raw is None else self._now(revoked_raw)
        if revoked_raw is not None and (revoked is None or revoked < created):
            return None
        live = record.get("live")
        upto = record.get("upto")
        if not isinstance(live, bool):
            return None
        if live:
            if upto is not None:
                return None
        elif (isinstance(upto, bool) or not isinstance(upto, int)
              or not 0 <= upto <= SHARE_MAX_MESSAGE_COUNT or upto % 2):
            return None
        return {
            "id": link_id,
            "session": session,
            "token_hash": token_hash,
            "created_at": created,
            "expires_at": expires,
            "revoked_at": revoked,
            "live": live,
            "upto": upto,
            "title": title,
        }

    def _paths_locked(self, *, expected_directory: Optional[Path] = None) -> list[Path]:
        """Enumerate the verified store, distinguishing absence from an unsafe failed scan.

        Returning an empty list for an I/O error is a privacy failure for live capabilities: owner
        routes would say "not shared", the owner could add new replies, and a temporarily unreadable
        link could become public again when the filesystem recovered.  A caller that already pinned a
        directory also needs disappearance/replacement during its operation to fail closed.
        """
        directory = self._safe_dir_locked()
        if directory is None:
            if expected_directory is not None or self._store_entry_exists_locked():
                raise self._store_unavailable()
            return []
        if expected_directory is not None and directory != expected_directory:
            raise self._store_unavailable()
        try:
            paths = sorted(directory.glob("*.json"))
        except (NotADirectoryError, OSError, RuntimeError) as exc:
            raise self._store_unavailable() from exc
        if self._safe_dir_locked() != directory:
            raise self._store_unavailable()
        return paths

    def _remove(self, path: Path) -> bool:
        path = self._safe_record_path_locked(path)
        if path is None:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _prune_locked(self, now: float, *, directory: Path,
                      aggressive: bool = False) -> None:
        """Bound the one-file registry without ever deleting an active capability.

        Expired and malformed records are already unauthorized and can be removed immediately.
        Revoked tombstones get a long normal retention window, but a capacity recovery may remove
        them earlier because their old token hash can never authenticate a future fresh-secret record.
        """
        for path in self._paths_locked(expected_directory=directory):
            record = self._validated_record(path, strict_io=True)
            if record is None or record["expires_at"] <= now:
                if not self._remove(path):
                    raise self._store_unavailable()
                continue
            revoked = record["revoked_at"]
            if revoked is not None and (
                    aggressive or now - revoked >= SHARE_REVOKED_RETENTION_SECONDS):
                if not self._remove(path):
                    raise self._store_unavailable()
        if self._safe_dir_locked() != directory:
            raise self._store_unavailable()

    def create(self, sid: str, *, message_count: int, title: str = "Shared chat",
               ttl_seconds: int = SHARE_DEFAULT_TTL_SECONDS, live: bool = False,
               now: Optional[float] = None) -> tuple[str, dict]:
        """Mint a link for `sid` and return `(token, public_record)`.

        `live=False` (the default) FREEZES the share at the turns that exist right now: `upto` is the
        transcript length at mint time and the reader never returns past it, so continuing the
        conversation cannot retroactively publish what the owner says next. `live=True` is the
        opt-in that keeps the link following the chat."""
        if isinstance(ttl_seconds, bool):
            raise ShareError("expiry must be a whole number of seconds")
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ShareError("expiry must be a whole number of seconds") from exc
        if isinstance(ttl_seconds, float) and not ttl_seconds.is_integer():
            raise ShareError("expiry must be a whole number of seconds")
        if not SHARE_MIN_TTL_SECONDS <= ttl <= SHARE_MAX_TTL_SECONDS:
            raise ShareError(
                f"expiry must be between {SHARE_MIN_TTL_SECONDS} and {SHARE_MAX_TTL_SECONDS} seconds")
        if not isinstance(sid, str) or _SESSION_ID_RE.fullmatch(sid) is None:
            raise ShareError("unknown Assistant session", code="assistant_share_session_invalid")
        if not isinstance(live, bool):
            raise ShareError("live must be a boolean")
        if (not isinstance(title, str) or not title.strip()
                or len(title) > SHARE_TITLE_MAX_CHARS):
            raise ShareError("share title is invalid", code="assistant_share_title_invalid")
        if (isinstance(message_count, bool) or not isinstance(message_count, int)
                or not 0 <= message_count <= SHARE_MAX_MESSAGE_COUNT or message_count % 2):
            raise ShareError(
                "this transcript is too large to publish safely",
                code="assistant_share_transcript_too_large", status_code=413)
        ts = self._now(now)
        if ts is None or not math.isfinite(ts + ttl):
            raise ShareError("share expiry is unavailable", code="assistant_share_time_unavailable",
                             status_code=503)
        with self._store_lock():
            directory = self._safe_dir_locked(create=True)
            if directory is None:
                raise self._store_unavailable()
            self._prune_locked(ts, directory=directory)
            if len(self._paths_locked(expected_directory=directory)) >= SHARE_MAX_RECORDS:
                self._prune_locked(ts, directory=directory, aggressive=True)
            if len(self._paths_locked(expected_directory=directory)) >= SHARE_MAX_RECORDS:
                raise ShareError(
                    "share capability capacity is full; revoke or wait for existing links to expire",
                    code="assistant_share_capacity", status_code=503)
            for _ in range(32):
                link_id = secrets.token_hex(16)
                path = self._path(link_id, directory)
                assert path is not None
                try:
                    # ``is_symlink`` catches a broken link for which ``exists`` is false.  Resolving
                    # the missing leaf also confirms that its parent has not been redirected since
                    # the directory boundary check above.
                    if (not path.exists() and not path.is_symlink()
                            and path.resolve(strict=False).parent == directory):
                        break
                except (NotADirectoryError, OSError, RuntimeError):
                    pass
                if self._safe_dir_locked() is None:
                    raise self._store_unavailable()
            else:  # practically unreachable, but never overwrite an existing capability on collision
                raise ShareError("could not reserve a unique share capability",
                                 code="assistant_share_capacity", status_code=503)
            # The secret is the whole capability; the id is only where the record lives.
            token = f"{link_id}.{secrets.token_urlsafe(32)}"
            record = {"id": link_id, "session": sid, "token_hash": self._digest(token),
                      "created_at": ts, "expires_at": ts + ttl, "revoked_at": None,
                      "live": live, "upto": None if live else message_count, "title": title}
            if self._safe_dir_locked() != directory:
                raise self._store_unavailable()
            atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True))
        return token, self.public(record)

    @staticmethod
    def public(record: dict) -> dict:
        """The exact owner-facing capability projection; never reflect unknown stored fields."""
        return {key: record.get(key) for key in (
            "id", "session", "created_at", "expires_at", "revoked_at", "live", "upto")}

    def resolve(self, token: str, *, now: Optional[float] = None) -> Optional[dict]:
        """The record this token grants, or None for unknown / expired / revoked / mismatched.

        One indistinguishable None for every failure: a reader must not be able to tell a revoked
        link from a never-existing one, which would turn this into a session-existence oracle."""
        token = str(token or "")
        link_id, separator, secret = token.partition(".")
        if (separator != "." or "." in secret or len(secret) != 43
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                       for c in secret)):
            return None
        ts = self._now(now)
        if ts is None:
            return None
        with self._lock:
            directory = self._safe_dir_locked()
            path = self._path(link_id, directory) if directory is not None else None
            if path is None:
                return None
            record = self._validated_record(path)
            if (record is None or ts < record["created_at"] or not secrets.compare_digest(
                    record["token_hash"], self._digest(token))
                    or record["revoked_at"] is not None or ts >= record["expires_at"]):
                return None
            return dict(record)

    def revoke_token(self, token: str, *, now: Optional[float] = None) -> bool:
        """Revoke exactly the freshly-minted capability named by ``token`` (rollback helper)."""
        token = str(token or "")
        link_id, separator, _secret = token.partition(".")
        ts = self._now(now)
        if separator != "." or ts is None:
            return False
        with self._store_lock():
            directory = self._safe_dir_locked()
            path = self._path(link_id, directory) if directory is not None else None
            if path is None:
                return False
            record = self._validated_record(path)
            if (record is None or not secrets.compare_digest(
                    record["token_hash"], self._digest(token))):
                return False
            if record["revoked_at"] is not None:
                return True
            record["revoked_at"] = max(ts, record["created_at"])
            if self._safe_record_path_locked(path) is None:
                return False
            atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True))
            return True

    def revoke_session(self, sid: str, *, now: Optional[float] = None) -> int:
        """Revoke EVERY link for `sid` and return how many were live.

        A tombstone stays for the bounded retention window, so ordinary stale clients keep resolving
        to nothing; later pruning is safe because a fresh record always has a fresh random id+secret.
        """
        if not isinstance(sid, str) or _SESSION_ID_RE.fullmatch(sid) is None:
            return 0
        ts = self._now(now)
        if ts is None:
            raise ShareError("share revocation time is unavailable",
                             code="assistant_unshare_time_unavailable", status_code=503)
        revoked = 0
        with self._store_lock():
            directory = self._safe_dir_locked()
            if directory is None:
                if self._store_entry_exists_locked():
                    raise self._store_unavailable()
                return 0
            self._prune_locked(ts, directory=directory)
            for path in self._paths_locked(expected_directory=directory):
                record = self._validated_record(path, strict_io=True)
                if (record is None or record["session"] != sid
                        or record["revoked_at"] is not None or record["expires_at"] <= ts):
                    continue
                record["revoked_at"] = max(ts, record["created_at"])
                if self._safe_record_path_locked(path) is None:
                    raise self._store_unavailable()
                atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True))
                revoked += 1
            if self._safe_dir_locked() != directory:
                raise self._store_unavailable()
        return revoked

    def active_for_session(self, sid: str, *, now: Optional[float] = None,
                           require_store: bool = False) -> list[dict]:
        if not isinstance(sid, str) or _SESSION_ID_RE.fullmatch(sid) is None:
            return []
        ts = self._now(now)
        if ts is None:
            raise ShareError(
                "share expiry cannot be determined safely",
                code="assistant_share_time_unavailable", status_code=503)
        out = []
        with self._lock:
            directory = self._safe_dir_locked()
            if directory is None:
                if require_store or self._store_entry_exists_locked():
                    raise self._store_unavailable()
                return []
            for path in self._paths_locked(expected_directory=directory):
                record = self._validated_record(path, strict_io=True)
                if (record is None or record["session"] != sid or ts < record["created_at"]
                        or record["revoked_at"] is not None or ts >= record["expires_at"]):
                    continue
                out.append(self.public(record))
            if self._safe_dir_locked() != directory:
                raise self._store_unavailable()
        return out

    def active_summary_by_session(self, *, now: Optional[float] = None) -> dict[str, dict]:
        """One bounded scan for owner session-list badges; returns no token or digest material."""
        ts = self._now(now)
        if ts is None:
            raise ShareError(
                "share expiry cannot be determined safely",
                code="assistant_share_time_unavailable", status_code=503)
        summary: dict[str, dict] = {}
        with self._lock:
            directory = self._safe_dir_locked()
            if directory is None:
                if self._store_entry_exists_locked():
                    raise self._store_unavailable()
                return {}
            for path in self._paths_locked(expected_directory=directory):
                record = self._validated_record(path, strict_io=True)
                if (record is None or ts < record["created_at"] or record["revoked_at"] is not None
                        or ts >= record["expires_at"]):
                    continue
                item = summary.setdefault(record["session"], {
                    "share_count": 0,
                    "share_ids": [],
                    "live_share_ids": [],
                    "share_expires_at": None,
                    "share_live": False,
                })
                item["share_count"] += 1
                # Exact capability ids let owner clients reconcile stale create/revoke responses
                # without ever exposing the bearer token or its digest.  Store capacity bounds this
                # list globally, and `_paths_locked` gives it deterministic id order.
                item["share_ids"].append(record["id"])
                if record["live"]:
                    item["share_live"] = True
                    item["live_share_ids"].append(record["id"])
                current_expiry = item["share_expires_at"]
                if current_expiry is None or record["expires_at"] > current_expiry:
                    item["share_expires_at"] = record["expires_at"]
            if self._safe_dir_locked() != directory:
                raise self._store_unavailable()
        return summary


# --------------------------------------------------------------------------- system prompt + toolset
def system_prompt(mode: str, *, repo_root: Path = REPO_ROOT, knowledge_dir: str | None = None,
                  cross_run_tools: bool = False, taxonomy_tools: bool = False) -> str:
    mode = normalize_mode(mode)
    mode_line = {
        "plan": "MODE=plan: you are READ-ONLY. You may inspect files and runs and PROPOSE changes in "
                "prose, but you cannot write files or run commands. Say what you would do.",
        "default": "MODE=default: read-only tools execute immediately; every mutating action (writing "
                   "a file, running a command, a git mutation, run control) pauses on a confirm card "
                   "and runs only if the user approves it.",
        "acceptEdits": "MODE=acceptEdits: file edits apply immediately; commands, git and launching a "
                       "run are still proposed for approval.",
        "auto": "MODE=auto: reversible edits and ordinary lifecycle actions may run directly. "
                "Shell, destructive, external, and unclassified actions still require explicit "
                "approval; do not claim they ran until the tool returns.",
    }[mode]
    return (
        "You are the LoopLab assistant — a capable coding/research agent embedded in the LoopLab Web "
        "UI. LoopLab is an autonomous ML research engine; you help the user do ANYTHING: understand and "
        "steer runs, work in their repos and data, and edit/repair LoopLab's OWN codebase.\n\n"
        + mode_line + "\n\n"
        f"The LoopLab source tree is at {repo_root}. You have read-only tools to inspect this machine "
        "(list_dir, read_file, find_files, grep) and to view runs (list_runs, read_run, read_run_experiment). "
        "To read what a node actually DID, you can go deeper: `read_run_logs` returns a node's captured "
        "stdout tail from training/eval plus its full error/stderr, and `read_run_trace` returns the "
        "node's agent trace as a linear conversation (the LLM's reasoning, outputs and tool calls that "
        "produced it). Ground your answers in what you actually read — inspect before you assert. When "
        "the user refers to a run, use list_runs/read_run to find and read it.\n"
        # E1: name the cross-run CONCEPT tools so the model reaches for them — they were wired but unnamed,
        # so it never used them. GATED on actual availability (both providers require memory_dir +
        # cross_run_read_tools), else the prompt would advertise tools absent from the schema and the model
        # would call a non-existent tool.
        + ("For cross-run KNOWLEDGE you have concept tools: `cross_run_concept_map` (the shared concept "
           "taxonomy across runs — which concepts appear most, their hierarchy, which pairs co-occur), "
           "`cross_run_atlas` / `cross_run_prior_attempts` / `cross_run_search` (what prior runs explored "
           "and found). Use these when a question spans runs or is about the concept graph.\n"
           if cross_run_tools else "")
        + ("" if not taxonomy_tools else
           "`concept_taxonomy` READS the editable shared concept taxonomy.\n" if mode == "plan" else
           "`concept_taxonomy` READS the editable shared concept taxonomy; you can CURATE it with "
           "concept_merge (fold a near-duplicate/rename), concept_split (coarse -> finer), concept_purge "
           "(tombstone) and concept_edit_clear (undo) — appended, reversible, mode/approver-gated.\n")
        + ("" if mode == "plan" else
           "You can also drive a run's LIFECYCLE directly: finalize_run (stop + wrap-up: report, "
           "lessons, cost), stop_run (freeze, no wrap-up), resume_run, reset_node (re-run a node in "
           "place from a stage), retag_node (replace one node's concept tags — the per-run counterpart to "
           "the cross-run concept_merge/split), set_run_concepts (set the run's BASE concept set that nodes "
           "inherit), and the DESTRUCTIVE delete_node / delete_run. And you can adjust a "
           "LIVE run's settings: extend_budget (more nodes/time — REOPENS a finished run so the budget "
           "is used), set_directive (a standing steer for the agents, e.g. 'use only sklearn'), and "
           "set_trust_gate (audit/gate/block). Each is gated by your mode and may raise a confirm card.\n")
        + "When the user wants to START a new autonomous-ML run, call `propose_run` with a run name + an "
        "inline COMPOSABLE `task` (goal + direction + the fields you have: repo / dataset / cmd / "
        "kaggle — there is NO `kind` field, the engine infers the task from what you describe) or a "
        "catalogue `task_file`, plus any settings (model, max_nodes) implied by their words — they get "
        "an editable launch card.\n"
        + (f"There is a shared KNOWLEDGE BASE at {knowledge_dir} — markdown notes that EVERY autonomous "
           "run's Researcher searches (via kb_search) to reuse past findings. When the user shares "
           "experiment results, lessons, recipes, or domain facts worth keeping across runs (e.g. an "
           "attached file describing past experiments and their metrics), DISTILL the essentials and "
           "save them with the `remember` tool so future runs benefit. `remember` changes the shared "
           "knowledge base, so it is unavailable in read-only plan mode and follows the active "
           "permission policy in mutating modes.\n"
           if knowledge_dir else "")
        + "Be concise and concrete; use Markdown. When you have the answer, call `final_answer` exactly "
        "once with your reply.")


# @-mentions: `@run:<id>` and `@file:<path>` in the user's message are expanded (server-side, before
# the model sees it) into grounding blocks — the OpenCode/Claude-Code pattern. The UI ALSO renders a
# live inline card for each `@run:<id>`, so a running run shows up right in the chat.
_MENTION = re.compile(r"""@(run|file):([^\s\])"'>}]+)""")


def expand_mentions(text: str, run_root, *, alive_fn: Optional[Callable] = None, roots=()) -> tuple:
    """Return (expanded_text, refs). For each @run:<id> append a run summary; for each @file:<path>
    append the file's contents (path/secret-gated via the read scout). `refs` lists what was
    referenced so the caller/UI can render live cards. Unknown/refused mentions are left as-is."""
    blocks, refs = [], []
    for m in _MENTION.finditer(text or ""):
        kind, raw = m.group(1), m.group(2).rstrip(".,;:!?)")
        if kind == "run":
            from looplab.tools.machine_runs_tools import MachineRunsTools
            summary = MachineRunsTools(run_root, alive_fn=alive_fn)._read_run(raw, "best", 6)
            if not summary.startswith("(no such run"):
                blocks.append(f"[@run:{raw}]\n{summary}")
                refs.append({"type": "run", "id": raw})
        elif kind == "file":
            from looplab.tools.reposcout import RepoScoutTools
            # Skip refused/unreadable files (outside roots, secret, missing) instead of embedding
            # the refusal string in the prompt — mirrors the @run branch and the docstring's promise.
            # ASK the scout rather than sniffing its output: the old shape test ("one parenthesized
            # line") also matched a genuine one-line stub like `(placeholder)`, which then vanished
            # with no grounding and no reason shown.
            ok, body = RepoScoutTools(
                list(roots) or [Path.home(), REPO_ROOT, Path(run_root)]).read_file_checked(raw)
            if ok:
                blocks.append(f"[@file:{raw}]\n```\n{body}\n```")
                refs.append({"type": "file", "path": raw})
    expanded = text if not blocks else (text + "\n\n--- Referenced context ---\n" + "\n\n".join(blocks))
    return expanded, refs


def _emit_spec() -> dict:
    return {"type": "function", "function": {
        "name": "final_answer",
        "description": "Provide your final reply to the user (Markdown). Call this exactly once when "
                       "you are done using tools.",
        "parameters": {"type": "object",
                       "properties": {"reply": {"type": "string"}}, "required": ["reply"]}}}


def build_tools(run_root, alive_fn: Optional[Callable] = None, mode: str = DEFAULT_MODE, *,
                approver: Optional[Callable] = None, trust_mode: str = "trusted_local", extra_roots=(),
                client=None, subagents: bool = False, mcp: bool = False, settings=None,
                on_todos: Optional[Callable] = None, cancel_check: Optional[Callable] = None,
                command_service=None, command_key_namespace: str = "",
                mutation_journal_path=None, mutation_recovery: bool = False):
    """The assistant's toolset. Read tools (filesystem scout, machine-run introspection, and — when
    memory_dir + cross_run_read_tools are on — the §22 cross-run concept/claims/atlas reads) are present
    in EVERY mode; the mutating write/shell/git providers are added only when the mode allows mutation
    (plan is read-only), mirroring "deny drops the tool from the schema". Each mutating provider gets
    the mode + the injected `approver` (which blocks on a UI confirm-card in `ask` situations).
    `subagents`/`mcp` add the `task` delegation tool and any configured MCP-server tools (top level
    only — a subagent runs with subagents=False to prevent unbounded nesting).

    A recovered dangling turn is deliberately narrower than its original toolset. Its model trace
    was lost, so write/shell/git/KB/MCP/subagent/proposal actions cannot be proven to match the first
    attempt. Recovery exposes only read tools, Todo, and (for a mutating persisted mode)
    RunControlTools backed by this turn's durable mutation journal. Missing journal identity means
    no run-control provider at all — recovery must never silently fall back to an unfenced one.
    """
    from looplab.agents.agent import CompositeTools
    from looplab.tools.reposcout import RepoScoutTools
    from looplab.tools.machine_runs_tools import (
        MachineRunsTools, RunControlTools, RunLauncherTools, TraceRewriteFns)

    def trace_rewrite_fns() -> TraceRewriteFns:
        # Composition boundary: the tool package remains below ``serve`` and receives the three
        # serving-owned trace-clear transaction primitives explicitly instead of importing upward.
        from looplab.serve import trace_clear
        return TraceRewriteFns(
            prepare_filtered_snapshot=trace_clear._prepare_filtered_trace_snapshot,
            digest_snapshot=trace_clear._trace_digest_snapshot,
            publish_prepared_snapshot=trace_clear._strict_replace_prepared_trace,
        )
    mode = normalize_mode(mode)
    roots = [Path.home(), REPO_ROOT, Path(run_root)] + list(extra_roots)
    providers = [RepoScoutTools(roots), MachineRunsTools(run_root, alive_fn=alive_fn)]
    mdir = getattr(settings, "memory_dir", None) if settings else None
    cross_run_enabled = bool(mdir and getattr(settings, "cross_run_read_tools", False))
    # Cross-run concept/claims/atlas READS — the same §22 portfolio knowledge the Researcher/Strategist
    # can ASK for mid-loop, now reachable by the owner assistant too (the operator asked for reading
    # tools). PORTFOLIO-WIDE: run_turn binds no single RunState, so the provider stays unbound (see its
    # bind_state docstring) and answers across the whole portfolio. Pure read, no mutation → present in
    # EVERY mode, incl. read-only plan and recovery; gated on the same memory_dir + flag as the roles.
    # CODEX AGENT: Multi-user security gap: ``cross_run_enabled`` is one process-wide feature flag, not
    # a principal/tenant authorization decision. Any caller that can reach the owner Assistant receives
    # the same unbound portfolio, including recovery and plan turns. Before shared deployment, pass an
    # authenticated visibility predicate into every provider read and redact before model/tool exposure.
    if cross_run_enabled:
        from looplab.tools.cross_run_tools import CrossRunTools
        providers.append(CrossRunTools(mdir, role="researcher"))
    if mutation_recovery:
        if mode != "plan" and mutation_journal_path is not None and command_key_namespace:
            providers.append(RunControlTools(
                run_root, alive_fn=alive_fn, mode=mode, approver=approver,
                command_service=command_service,
                command_key_namespace=command_key_namespace,
                mutation_journal_path=mutation_journal_path,
                mutation_recovery=True,
                trace_rewrite=trace_rewrite_fns()))
        providers.append(TodoTools(on_todos=on_todos))
        return CompositeTools(providers)

    providers.append(RunLauncherTools())
    if cross_run_enabled:
        # PART V §22.4 (Phase 2): edit the shared cross-run concept TAXONOMY (merge/rename/purge/split)
        # via the append-only, reversible governance ledger. The provider itself gates: it contributes
        # only the read `concept_taxonomy` in plan mode and adds the mutation verbs (mode+approver gated,
        # like the KB/write tools) in mutating modes — so it is safe to add in every non-recovery mode.
        # the documented flag is the owner Assistant's portfolio ACL/redaction
        # kill-switch. It must gate taxonomy reads and shared governance mutations as well as the
        # sibling CrossRunTools reads; memory_dir alone is storage configuration, not authorization.
        from looplab.tools.concept_tools import ConceptGovernanceTools
        providers.append(ConceptGovernanceTools(mdir, mode=mode, approver=approver))
    kdir = getattr(settings, "knowledge_dir", None) if settings else None
    if kdir and mode != "plan":                         # shared KB append is a real mutation
        from looplab.tools.knowledge_tools import KnowledgeWriteTools
        providers.append(KnowledgeWriteTools(kdir, mode=mode, approver=approver))
    if mode != "plan":
        from looplab.tools.write_tools import WriteTools
        from looplab.tools.shell_tools import ShellTools
        from looplab.tools.git_tools import GitTools
        sh = ShellTools(roots, mode=mode, trust_mode=trust_mode, approver=approver,
                        default_cwd=REPO_ROOT)   # the spec promises "default: repo root", not $HOME
        backup_dir = Path(run_root) / "assistant" / "backups"
        providers += [WriteTools(roots, mode=mode, approver=approver, repo_root=REPO_ROOT,
                                 backup_dir=backup_dir),
                      sh, GitTools(sh, cwd=REPO_ROOT),
                      # Drive an existing run's lifecycle (finalize/stop/resume/reset/delete node/run),
                      # self-gated by the same mode+approver so destructive verbs raise a confirm card.
                      RunControlTools(run_root, alive_fn=alive_fn, mode=mode, approver=approver,
                                      command_service=command_service,
                                      command_key_namespace=command_key_namespace,
                                      mutation_journal_path=mutation_journal_path,
                                      mutation_recovery=mutation_recovery,
                                      trace_rewrite=trace_rewrite_fns())]
    providers.append(TodoTools(on_todos=on_todos))
    if subagents and client is not None:
        providers.append(SubagentTools(client, run_root, alive_fn=alive_fn, settings=settings,
                                       cancel_check=cancel_check))
    if mcp and mode != "plan":
        # MCP tools are arbitrary external side effects: never in read-only plan mode (which also keeps
        # connecting/spawning a configured stdio MCP server out of a read-only session), and always
        # behind the permission policy — CompositeTools dispatched them unpoliced before (P0-6).
        try:
            from looplab.tools.mcp_tools import McpTools, GatedMcpTools
            m = McpTools.cached()      # connect to MCP servers ONCE per process, not per turn
            if m.specs():
                providers.append(GatedMcpTools(m, mode=mode, approver=approver))
        except Exception:  # noqa: BLE001 - MCP is optional; never break the toolset
            pass
    return CompositeTools(providers)


FINAL_ANSWER_DIRECTIVE = (
    "Now write your final answer to the user, in Markdown.\n"
    "\n"
    "Answer the LAST user message only, and report only the work you did in THIS turn — everything "
    "after that message. The earlier turns above are context you may rely on; do not re-summarize "
    "them, do not recap what you did in previous turns, and do not open with a summary of the "
    "conversation so far. Be concise."
)


def final_answer_messages(convo: list, *, directive: str = FINAL_ANSWER_DIRECTIVE) -> list:
    """The message list for the streamed FINAL answer.

    The whole conversation stays as CONTEXT — a turn routinely depends on a file read or a decision
    from three turns ago, and trimming it makes the model answer worse. What changed is the
    directive: it used to say "based on everything above", so the model dutifully summarized the
    entire dialog and every turn's answer re-narrated the session. The operator's report was exactly
    that, and it is a prompt bug rather than a context bug.

    The boundary is the LAST user message, which `run_turn` appends as this turn's instruction: the
    work of this turn is everything after it. Marked explicitly rather than left implicit, because
    "the last user message" is a thing the model has to FIND in a trace that may hold dozens of tool
    results, and a trace with no user message at all (a subagent, a replayed fragment) must still
    produce an answer rather than a broken pointer.
    """
    marked = list(convo)
    last_user = next((i for i in range(len(marked) - 1, -1, -1)
                      if (marked[i] or {}).get("role") == "user"), None)
    if last_user is not None:
        body = (marked[last_user] or {}).get("content") or ""
        marked[last_user] = {**marked[last_user],
                             "content": f"[current turn — answer this]\n{body}"}
    return marked + [{"role": "user", "content": directive}]


def run_turn(client, run_root, messages: list, instruction: str, mode: str = DEFAULT_MODE, *,
             alive_fn: Optional[Callable] = None, settings=None, on_step: Optional[Callable] = None,
             approver: Optional[Callable] = None, extra_roots=(), _subagent: bool = False,
             on_todos: Optional[Callable] = None, reply_sink: Optional[Callable] = None,
             on_text: Optional[Callable] = None, cancel_check: Optional[Callable] = None,
             command_service=None, command_key_namespace: str = "",
             mutation_journal_path=None, mutation_recovery: bool = False) -> dict:
    """Run ONE assistant turn: drive the shared tool loop over the mode's toolset and return a
    response dict {ok, reply, steps, applied, mode}. `messages` is the prior conversation
    (role/content); `instruction` is the new user message. Pure orchestration — the caller injects the
    LLM client, the run-liveness probe, Settings and the `approver` (so it is unit-testable with a
    scripted fake client + a stub approver)."""
    from looplab.agents.agent import LoopOptions, drive_tool_loop, loop_opts_from_settings
    mode = normalize_mode(mode)
    trust_mode = getattr(settings, "trust_mode", "trusted_local") if settings is not None else "trusted_local"
    tools = build_tools(run_root, alive_fn=alive_fn, mode=mode, approver=approver,
                        trust_mode=trust_mode, extra_roots=extra_roots,
                        client=client, subagents=not _subagent, mcp=not _subagent, settings=settings,
                        on_todos=on_todos, cancel_check=cancel_check,
                        command_service=command_service,
                        command_key_namespace=command_key_namespace,
                        mutation_journal_path=mutation_journal_path,
                        mutation_recovery=mutation_recovery)
    roots = [Path.home(), REPO_ROOT, Path(run_root)] + list(extra_roots)
    from looplab.serve.assistant_commands import expand_command
    grounded, refs = expand_mentions(expand_command(instruction), run_root, alive_fn=alive_fn, roots=roots)
    # Mirror build_tools' gating so the prompt only names tools that are actually registered.
    _mdir = getattr(settings, "memory_dir", None) if settings else None
    _has_cross_run = bool(_mdir and getattr(settings, "cross_run_read_tools", False))
    _has_taxonomy = _has_cross_run
    convo = [{"role": "system", "content": system_prompt(
        mode, knowledge_dir=(getattr(settings, "knowledge_dir", None) if settings else None),
        cross_run_tools=_has_cross_run, taxonomy_tools=_has_taxonomy)}]
    for m in messages:
        role = m.get("role")
        # A user turn may carry `raw` — the full model-facing instruction (attached-file contents,
        # UI-context preamble) persisted alongside the clean display `content`. Prefer it, or the
        # model loses the attachments on every turn after the one they were sent with (the browser
        # is the only other place that content exists).
        body = (m.get("raw") or m.get("content")) if role == "user" else m.get("content")
        # A TYPED @mention (`@file:…`/`@run:…`) has display==instruction, so no `raw` was persisted —
        # the stored `content` is only the literal mention text, and the grounding (file body / run
        # summary) the model saw on the original turn would be LOST on every later turn. Re-expand a
        # historical user turn's mentions here (skip turns that already carry a grounded `raw`) so the
        # context stays present — the same asymmetry the `raw` mechanism fixed for attachments.
        # Gated on the mention itself, NOT on `raw` being absent. `raw` holds the UNEXPANDED
        # instruction (the router persists it pre-expand) and `expand_mentions`' output is never
        # stored, so a turn that carried a UI-context preamble — hence a `raw` — AND an @run/@file
        # mention lost its grounding on every later turn: precisely the failure this block exists to
        # fix, just through the other door.
        if role == "user" and body and "@" in body:
            try:
                body, _ = expand_mentions(body, run_root, alive_fn=alive_fn, roots=roots)
            except Exception:  # noqa: BLE001 - grounding re-expansion is best-effort
                pass
        if role in ("user", "assistant") and body:
            convo.append({"role": role, "content": body})
    convo.append({"role": "user", "content": grounded})

    steps: list[dict] = []

    def _on_step(ev: dict) -> None:
        label = _step_label(ev)
        steps.append({"tool": (ev or {}).get("tool", ""), "arg": str((ev or {}).get("arg", "")),
                      "label": label, "turn": (ev or {}).get("turn", 0)})
        if on_step is not None:
            try:
                on_step({**(ev or {}), "label": label})
            except Exception:  # noqa: BLE001 - progress must never perturb the loop
                pass

    box: dict = {}

    def _fin(args):
        box["reply"] = (args or {}).get("reply", "") if isinstance(args, dict) else ""
        return box["reply"]

    def _fb(msgs):
        if box.get("reply"):
            return box["reply"]
        for m in reversed(msgs):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
                return m["content"].strip()
        return "(no reply)"

    opts = LoopOptions.coerce(loop_opts_from_settings(settings) if settings is not None else None)
    max_turns = int(getattr(settings, "agent_max_turns", 0) or 0)
    # Interactive assistant: bound the turn's wall-clock so a stalled shared-LLM call can't leave the
    # chat "thinking" forever. Falls back to 5 min when the setting is unset (0 = unlimited).
    time_budget = float(getattr(settings, "agent_time_budget_s", 0.0) or 0.0) or 300.0
    # `.replace()` (this WINS) for all three: the assistant uses the visible write_todos tool instead
    # of the loop's self-plan, and its 5-minute floor is deliberately not the configured value.
    # Folding them into the bundle is what stops `max_turns=…, **opts` — the shape that raises
    # `TypeError: got multiple values` the day the bundle grows a limit (doc 25 AG-01).
    opts = opts.replace(self_plan=False, max_turns=max_turns, time_budget_s=time_budget)
    def _collect(attr):
        return [a for p in getattr(tools, "providers", []) if hasattr(p, attr) for a in getattr(p, attr)]

    # WHAT HAPPENED TO MY TURN. On budget exhaustion the loop salvages one forced emit from what it
    # gathered — the right move — but presenting a cut-short investigation as a finished answer is
    # how "the assistant hangs around 40 tool uses and then something odd comes back" reads to an
    # operator who was never told the turn ran out of wall clock. `agent_time_budget_s` is unset by
    # default, so the 5-minute floor below is what most turns actually hit.
    budget_box: dict = {}
    try:
        reply = drive_tool_loop(client, tools, convo, _emit_spec(),
                                finalize=_fin, fallback=_fb, on_step=_on_step, on_text=on_text,
                                cancel_check=cancel_check, on_budget=budget_box.update, **opts)
    except Exception as e:  # noqa: BLE001 - surface a usable error, never crash the request
        return {"ok": False, **safe_assistant_failure(e), "steps": steps,
                "applied": _collect("applied"), "proposals": _collect("proposals"),
                "todos": _collect("todos"), "refs": refs, "mode": mode}
    reply = reply or box.get("reply") or "(no reply)"
    # Real token streaming of the FINAL answer: after the tool loop has acted, generate the
    # user-facing answer with a streaming call over the accumulated trace, pushing tokens to the sink.
    # (One extra call; reuses the context the loop built. The loop's emit reply is the fallback.)
    # GUARD (belt): drive_tool_loop compacts a long trace IN PLACE (slice-assign), so `convo` stays
    # current through auto_summary. If tools nonetheless ran and `convo` holds no tool-result
    # messages (compaction summarized every one away), streaming over it would make the model
    # re-answer BLIND — skip streaming and keep the loop's (correct) reply.
    trace_ok = (not steps) or any(m.get("role") == "tool" for m in convo)
    # If the user cancelled, DON'T fire a fresh (un-cancellable) streaming completion for the final
    # answer — that call could hang on the shared LLM and keep the worker (and its SSE stream) alive
    # long after Stop. Keep the loop's already-computed reply instead.
    try:
        _cancelled = bool(cancel_check and cancel_check())
    except Exception:  # noqa: BLE001 - a broken cancel probe must not discard a computed reply
        _cancelled = False
    if reply_sink is not None and trace_ok and not _cancelled:
        try:
            # Strip UNANSWERED tool calls anywhere in the trace, not just a trailing message:
            # when the model paired a retrieval call with final_answer, the loop executed the
            # retrieval but returned on final_answer, leaving its tool_call_id dangling — strict
            # OpenAI-compatible endpoints 400 on that and the turn silently loses streaming.
            answered = {m.get("tool_call_id") for m in convo if m.get("role") == "tool"}
            base = []
            for m in convo:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    kept = [c for c in m["tool_calls"] if c.get("id") in answered]
                    if not kept and not (m.get("content") or "").strip():
                        continue
                    if len(kept) != len(m["tool_calls"]):
                        m = {**m, "tool_calls": kept} if kept else \
                            {k: v for k, v in m.items() if k != "tool_calls"}
                base.append(m)
            # SCOPED to this turn. The old directive pointed the model at the entire trace, so every
            # turn's answer re-summarized the session — see `final_answer_messages`, which owns the
            # directive and the current-turn boundary so both are statable and testable.
            stream_msgs = final_answer_messages(base)
            streamed = []
            for piece in client.complete_text_stream(stream_msgs):
                if cancel_check and cancel_check():   # stop honored mid-stream too
                    break
                streamed.append(piece)
                try:
                    reply_sink(piece)
                except Exception:  # noqa: BLE001 - a sink failure must not abort the turn
                    pass
            if "".join(streamed).strip():
                reply = "".join(streamed)
        except Exception:  # noqa: BLE001 - streaming is an enhancement; keep the loop's reply
            pass
    # AFTER the streaming block, never before it: that block REPLACES `reply` wholesale with the
    # streamed answer, so a notice appended earlier is silently dropped on the ordinary path (a
    # reply_sink is present, the turn was not cancelled, the trace is intact). The envelope key
    # survived and the sentence the operator reads did not — which is the entire point of it.
    if budget_box:
        limit = "wall-clock budget" if budget_box.get("kind") == "time" else "turn budget"
        reply = (f"{reply}\n\n---\n_This turn hit its {limit} after "
                 f"{budget_box.get('turns')} tool turns ({budget_box.get('seconds')}s) and was cut "
                 f"short — the answer above is the best I could assemble from what I had gathered, "
                 f"not a finished investigation. Ask me to continue, or raise "
                 f"`agent_time_budget_s`._")
    return {"ok": True, "reply": reply, "steps": steps,
            "applied": _collect("applied"), "proposals": _collect("proposals"),
            "todos": _collect("todos"), "refs": refs, "mode": mode,
            # Absent on an ordinary turn. Present, with its kind and numbers, when the answer above
            # is a salvage rather than a conclusion — so the UI can say so instead of the operator
            # inferring it from a reply that stops early.
            **({"budget_exhausted": dict(budget_box)} if budget_box else {})}


def _step_label(ev: dict) -> str:
    tool = (ev or {}).get("tool", "")
    arg = str((ev or {}).get("arg", ""))
    short = arg.rsplit("/", 1)[-1] if arg else ""
    return ({"read_file": f"reading {short}", "list_dir": f"listing {short or 'a directory'}",
             "find_files": f"searching {short or 'files'}",
             "list_runs": "listing runs", "read_run": f"reading run {short or ''}".strip(),
             "task": "delegating a subtask"}.get(tool)
            or (f"{tool} {short}".strip() if tool else "thinking"))


class TodoTools:
    """A visible TODO list for multi-step work (Claude-Code TodoWrite). The model calls `write_todos`
    to keep an up-to-date checklist; the latest list is surfaced live to the UI (via `on_todos`) and
    returned with the turn, so a long task shows its plan and progress instead of an opaque wait."""

    def __init__(self, on_todos: Optional[Callable] = None):
        self.todos: list[dict] = []
        self.on_todos = on_todos

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        from looplab.tools._base import fn_spec
        return [fn_spec(
            "write_todos",
            "Record/update your TODO list for a multi-step task (replaces the previous list). Mark each "
            "item pending / in_progress / completed as you go. Use it for any task with 3+ steps.",
            {"todos": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}}}},
            ["todos"])]

    def execute(self, name: str, args: dict) -> str:
        if name != "write_todos":
            return f"(unknown tool: {name})"
        items = [{"content": str(t.get("content", "")), "status": t.get("status", "pending")}
                 for t in ((args or {}).get("todos") or []) if isinstance(t, dict)]
        self.todos = items
        if self.on_todos:
            try:
                self.on_todos(items)
            except Exception:  # noqa: BLE001 - live surface is best-effort
                pass
        done = sum(1 for t in items if t["status"] == "completed")
        return f"(todos updated: {done}/{len(items)} done)"


class SubagentTools:
    """Delegate a self-contained subtask to a FRESH agent with its own context (Claude-Code `task`).
    The subagent runs a full read-only inner turn and returns ONLY its final text — the token-saving
    point (the main loop never sees the subagent's intermediate tool churn). Runs in `plan` mode
    (read-only) so a subagent can research/inspect freely without mutating behind the user's back; the
    main agent applies any change itself (under the user's mode/approval). Nesting is prevented — the
    inner turn is built with subagents=False."""

    def __init__(self, client, run_root, alive_fn: Optional[Callable] = None, settings=None,
                 cancel_check: Optional[Callable] = None):
        self.client = client
        self.run_root = run_root
        self.alive_fn = alive_fn
        self.settings = settings
        self.cancel_check = cancel_check   # forwarded so Stop interrupts a long-running subagent too

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        from looplab.tools._base import fn_spec
        return [fn_spec(
            "task",
            "Delegate a focused, self-contained subtask to a fresh sub-agent that has its OWN context "
            "and read-only tools (inspect files, read runs). It returns only its final answer — use it "
            "to research/summarize a big area without cluttering your own context. Give a complete, "
            "standalone prompt.",
            {"prompt": {"type": "string", "description": "the full standalone subtask"}},
            ["prompt"])]

    def execute(self, name: str, args: dict) -> str:
        if name != "task":
            return f"(unknown tool: {name})"
        prompt = str((args or {}).get("prompt") or "").strip()
        if not prompt:
            return "(task needs a prompt)"
        # Bail immediately if the user already hit Stop before this subtask even began.
        if self.cancel_check is not None:
            try:
                if self.cancel_check():
                    return "(cancelled by the user)"
            except Exception:  # noqa: BLE001 - a broken cancel probe must not block the subtask
                pass
        # Inner turn: read-only, no further subagents (build_tools called with subagents=False by
        # passing client=None to the recursive run_turn's build — enforced by _subagent flag).
        # Forward cancel_check so Stop interrupts the inner loop at its next turn boundary instead of
        # letting it run its full time-budget while the outer UI is already dead.
        res = run_turn(self.client, self.run_root, [], prompt, "plan",
                       alive_fn=self.alive_fn, settings=self.settings, _subagent=True,
                       cancel_check=self.cancel_check)
        return res.get("reply") or "(subagent returned nothing)"
