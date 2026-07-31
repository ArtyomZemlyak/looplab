"""Persistent, revocable read-only capabilities for sharing one run.

The normal UI token identifies the operator control plane.  A review link is deliberately a
different credential: it names exactly one run, expires, can be revoked, and is accepted only for
an allow-listed set of GET projections.  Bearer values are never persisted; ``reviews.json`` keeps
only a SHA-256 digest plus non-secret metadata used by the owner-facing link manager.  Each link is
one atomic JSON file, so multiple server workers cannot lose one another's creates by racing on a
single read/modify/write document.

This is a capability boundary for the review surface, not a replacement for deployment identity or
RBAC.  In particular, an otherwise unauthenticated LoopLab deployment remains unauthenticated when
someone ignores the review URL and calls its owner API directly.  Deployments exposed to other
principals must still protect the owner UI/control plane as described in the deployment guide.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import time
import uuid
from pathlib import Path

from looplab.core.atomicio import atomic_write_text


DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
MIN_TTL_SECONDS = 5 * 60
MAX_TTL_SECONDS = 30 * 24 * 60 * 60
REVIEW_HEADER = "X-LoopLab-Review"
_GENERATION_HEX = frozenset("0123456789abcdef")
_RECOVERY_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_CREATE_CONTRACT = 1
_STORE_LOCK_TIMEOUT_SECONDS = 5.0
_STORE_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: dict[str, threading.Lock] = {}
_CURRENT_GENERATION_UNSET = object()


class ReviewError(ValueError):
    """Invalid review-link operation or credential."""

    def __init__(self, message: str, *, kind: str = "invalid", **metadata):
        super().__init__(message)
        self.kind = kind
        self.metadata = metadata


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def exact_review_request_id(value: object) -> str | None:
    """Return only a canonical lowercase RFC 4122 UUIDv4 create identity."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if (parsed.variant == uuid.RFC_4122 and parsed.version == 4
                     and str(parsed) == value) else None


def exact_review_token_secret(value: object) -> bytes | None:
    """Decode only a canonical unpadded base64url 256-bit create-recovery secret."""
    if not isinstance(value, str) or _RECOVERY_SECRET.fullmatch(value) is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, TypeError):
        return None
    if len(decoded) != 32:
        return None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    return decoded if hmac.compare_digest(canonical, value) else None


def _recovery_identity(run_id: str, request_id: str) -> tuple[str, str]:
    identity = _canonical_bytes({
        "request_id": request_id,
        "run_id": run_id,
        "v": _CREATE_CONTRACT,
    })
    identity_hash = hashlib.sha256(
        b"looplab-review-create-id-v1\0" + identity).hexdigest()
    return "rvl_" + identity_hash[:32], identity_hash


def _recovery_token(link_id: str, token_secret: bytes) -> str:
    suffix = link_id[4:]
    material = hmac.new(
        token_secret, b"looplab-review-bearer-v1\0" + link_id.encode("ascii"),
        hashlib.sha256).digest()
    bearer = base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")
    return f"rv_{suffix}_{bearer}"


def _recovery_intent(run_id: str, generation: str, ttl_seconds: int,
                     include_evidence: bool) -> str:
    intent = _canonical_bytes({
        "expected_generation": generation,
        "include_evidence": include_evidence,
        "run_id": run_id,
        "ttl_seconds": ttl_seconds,
        "v": _CREATE_CONTRACT,
    })
    return hashlib.sha256(b"looplab-review-create-intent-v1\0" + intent).hexdigest()


def _finite_number(value, fallback: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    # Python's JSON reader accepts NaN/Infinity.  Never pass those to a JSON response and never let
    # NaN's always-false comparisons turn a corrupt expiry into a live capability.
    return number if math.isfinite(number) else fallback


def exact_review_generation(value: object) -> str | None:
    """Return only the canonical event-log generation spelling used by run commands."""
    return (value if isinstance(value, str) and len(value) == 64
            and all(char in _GENERATION_HEX for char in value) else None)


def _public(record: dict) -> dict:
    """Owner/reviewer-safe metadata (never return the persisted credential digest)."""
    scopes = record.get("scopes")
    safe_scopes = ([scope for scope in scopes
                    if isinstance(scope, str) and scope in {"summary", "evidence"}]
                   if isinstance(scopes, list) else [])
    revoked = record.get("revoked_at")
    return {
        "id": record.get("id") if isinstance(record.get("id"), str) else "",
        "run_id": record.get("run_id") if isinstance(record.get("run_id"), str) else "",
        "generation": exact_review_generation(record.get("generation")),
        "scopes": safe_scopes,
        "created_at": _finite_number(record.get("created_at"), 0.0),
        "expires_at": _finite_number(record.get("expires_at"), 0.0),
        # Any malformed non-null revocation marker stays revoked (epoch 0), never active.
        "revoked_at": (None if revoked is None else _finite_number(revoked, 0.0)),
    }


class ReviewStore:
    """One-file-per-capability persistent store."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._lock = self._process_lock()
        # Keep the coordination inode outside the atomically-created capability directory. It is a
        # permanent sibling and must never be deleted or replaced by record cleanup.
        self._lock_path = Path(str(self.directory) + ".lock")

    def _path(self, link_id: str) -> Path:
        # New ids carry 128 bits.  Keep accepting the 48-bit ids produced by the first SAFE-01
        # implementation so an upgrade does not strand links created during development.
        suffix = link_id[4:] if link_id.startswith("rvl_") else ""
        if len(suffix) not in {12, 32} or not all(c in "0123456789abcdef" for c in suffix):
            raise ReviewError("no such review link", kind="not_found")
        return self.directory / f"{link_id}.json"

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _number(value, fallback: float = 0.0) -> float:
        number = _finite_number(value, fallback)
        return fallback if number is None else number

    @staticmethod
    def _save(path: Path, record: dict) -> None:
        atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True))

    def _process_lock(self) -> threading.Lock:
        # abspath is lexical and cannot turn a transient/missing store directory into a startup I/O
        # failure; endpoint-time filesystem failures are translated to the structured storage error.
        key = os.path.normcase(os.path.abspath(os.fspath(self.directory)))
        with _STORE_LOCKS_GUARD:
            return _STORE_LOCKS.setdefault(key, threading.Lock())

    @contextmanager
    def _store_lock(self):
        """Require one lock for deterministic ids across every run and server worker.

        The run sequencer cannot protect the global ``.reviews`` namespace: two different runs hold
        different command locks while their truncated on-disk ids could still collide.  Pair a
        process-wide lock with a required OS lock, and fail closed when the filesystem cannot provide
        that ordering.  There is intentionally no thread-only fallback.
        """
        from looplab.events.eventstore import (
            EventStoreLockError, InterprocessLockContended, _interprocess_lock)

        if not self._lock.acquire(timeout=_STORE_LOCK_TIMEOUT_SECONDS):
            raise ReviewError(
                "timed out waiting for the review-link store lock", kind="storage")
        try:
            try:
                # Non-blocking acquisition gives the HTTP layer a bounded, safely retryable 503
                # instead of parking a request thread behind another worker indefinitely.
                with _interprocess_lock(
                        self._lock_path, required=True, blocking=False):
                    yield
            except (EventStoreLockError, InterprocessLockContended, OSError) as exc:
                raise ReviewError(
                    "review-link storage is temporarily unavailable", kind="storage") from exc
        finally:
            self._lock.release()

    @staticmethod
    def _ttl(value, *, strict: bool = False) -> int:
        if strict and type(value) is not int:
            raise ReviewError(
                "expiry must be a whole JSON number of seconds", kind="invalid_recovery")
        try:
            ttl = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReviewError("expiry must be a whole number of seconds") from exc
        if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
            raise ReviewError(
                f"expiry must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds")
        return ttl

    @staticmethod
    def _record(run_id: str, generation: str, ttl: int, include_evidence: bool,
                link_id: str, token: str, *, now: float | None = None, **extra) -> dict:
        created_at = time.time() if now is None else now
        return {
            "id": link_id,
            "token_hash": _digest(token),
            "run_id": run_id,
            "generation": generation,
            "scopes": ["summary"] + (["evidence"] if include_evidence else []),
            "created_at": created_at,
            "expires_at": created_at + ttl,
            "revoked_at": None,
            **extra,
        }

    @staticmethod
    def _remove_failed_reservation(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    @staticmethod
    def _strict_existing(path: Path) -> tuple[str, dict | None]:
        """Distinguish absence, an abandoned reservation, and corrupt persisted state.

        A rolling upgrade may overlap an older worker which reserves the final path before its atomic
        replace and does not know about the new store lock. Give that narrow empty-file window a
        bounded chance to finish. Non-empty malformed state is never treated as free space.
        """
        for attempt in range(21):
            try:
                info = path.lstat()
            except FileNotFoundError:
                return "absent", None
            except OSError:
                return "unreadable", None
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return "corrupt", None
            if info.st_size != 0:
                try:
                    raw = path.read_text(encoding="utf-8")
                    value = json.loads(raw)
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    return "corrupt", None
                return ("valid", value) if isinstance(value, dict) else ("corrupt", None)
            if attempt < 20:
                time.sleep(0.01)
        age = max(0.0, time.time() - info.st_mtime)
        return ("abandoned_empty" if age >= 1.0 else "fresh_empty"), None

    def _publish_reserved(self, path: Path, record: dict) -> None:
        """Publish one owned reservation, preserving any non-empty uncertain result."""
        try:
            self._save(path, record)
            return
        except Exception as exc:  # noqa: BLE001 - normalize storage failures without leaking paths
            state, existing = self._strict_existing(path)
            if state == "valid" and existing == record:
                # os.replace completed before a later filesystem operation reported failure.
                return
            if state in {"fresh_empty", "abandoned_empty"}:
                # This caller created the reservation and still holds the global transaction lock.
                self._remove_failed_reservation(path)
            raise ReviewError("review-link storage is unavailable", kind="storage") from exc

    def _reserve(self) -> tuple[str, Path]:
        """Atomically reserve a fresh id without ever overwriting an existing capability.

        Randomness makes a collision extraordinarily unlikely, but relying on probability alone
        would let a collision replace an existing token digest.  ``O_EXCL`` also makes this safe
        across threads and multiple server workers.  The empty reservation is fail-closed if the
        process crashes before the atomic JSON replacement.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        for _ in range(128):
            link_id = "rvl_" + secrets.token_hex(16)
            path = self._path(link_id)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(fd)
            return link_id, path
        raise ReviewError("could not allocate a unique review link")

    def _reserve_exact(self, link_id: str) -> tuple[Path, bool]:
        path = self._path(link_id)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return path, False
        os.close(fd)
        return path, True

    def create(self, run_id: str, *, generation: str,
               ttl_seconds: int = DEFAULT_TTL_SECONDS,
               include_evidence: bool = False) -> tuple[str, dict]:
        ttl = self._ttl(ttl_seconds)
        exact_generation = exact_review_generation(generation)
        if exact_generation is None:
            raise ReviewError(
                "run generation is missing or malformed", kind="generation")
        with self._store_lock():
            try:
                link_id, path = self._reserve()
                token = f"rv_{link_id[4:]}_{secrets.token_urlsafe(32)}"
                record = self._record(
                    str(run_id), exact_generation, ttl, bool(include_evidence), link_id, token)
                self._publish_reserved(path, record)
            except ReviewError:
                raise
            except OSError as exc:
                raise ReviewError(
                    "review-link storage is unavailable", kind="storage") from exc
        return token, _public(record)

    @staticmethod
    def _valid_recovery_record(record: dict, *, link_id: str, run_id: str,
                               generation: str, ttl: int,
                               include_evidence: bool) -> bool:
        scopes = record.get("scopes")
        revoked = record.get("revoked_at")
        created_value = record.get("created_at")
        expires_value = record.get("expires_at")
        created_at = (_finite_number(created_value)
                      if type(created_value) in {int, float} else None)
        expires_at = (_finite_number(expires_value)
                      if type(expires_value) in {int, float} else None)
        valid_revocation = (revoked is None or (
            type(revoked) in {int, float} and _finite_number(revoked) is not None))
        expected_scopes = ["summary"] + (["evidence"] if include_evidence else [])
        return (
            record.get("id") == link_id
            and record.get("run_id") == run_id
            and record.get("generation") == generation
            and scopes == expected_scopes
            and created_at is not None
            and expires_at is not None
            and expires_at >= created_at
            and math.isclose(expires_at - created_at, ttl, rel_tol=0.0, abs_tol=1e-6)
            and valid_revocation
        )

    def create_or_replay(self, run_id: str, *, generation: str,
                         expected_generation: str, request_id: str,
                         token_secret: bytes | str,
                         ttl_seconds: int = DEFAULT_TTL_SECONDS,
                         include_evidence: bool = False) -> tuple[str, dict, bool]:
        """Create once or reconstruct the exact bearer from a client-held recovery envelope.

        The full identity, intent, and token hashes are durable collision/conflict checks. The path
        uses only 128 bits so it retains the existing token format, but a truncated-prefix collision
        can never be accepted as a replay. Existing records are resolved before the current generation
        is fenced, allowing a lost response to recover its original (possibly now stale) capability.
        """
        canonical_run = str(run_id)
        canonical_request = exact_review_request_id(request_id)
        if canonical_request is None:
            raise ReviewError("invalid review create request", kind="invalid_recovery")
        if isinstance(token_secret, bytes):
            secret = token_secret if len(token_secret) == 32 else None
        else:
            secret = exact_review_token_secret(token_secret)
        if secret is None:
            raise ReviewError("invalid review create request", kind="invalid_recovery")
        expected = exact_review_generation(expected_generation)
        if expected is None or type(include_evidence) is not bool:
            raise ReviewError("invalid review create request", kind="invalid_recovery")
        ttl = self._ttl(ttl_seconds, strict=True)
        link_id, identity_hash = _recovery_identity(canonical_run, canonical_request)
        intent_hash = _recovery_intent(
            canonical_run, expected, ttl, include_evidence)
        token = _recovery_token(link_id, secret)
        token_hash = _digest(token)
        path = self._path(link_id)

        with self._store_lock():
            state, existing = self._strict_existing(path)
            if state == "valid":
                assert existing is not None
                contract_value = existing.get("create_contract")
                contract_matches = (type(contract_value) is int
                                    and contract_value == _CREATE_CONTRACT)
                identity_matches = isinstance(existing.get("create_identity_hash"), str) \
                    and hmac.compare_digest(existing["create_identity_hash"], identity_hash)
                intent_matches = isinstance(existing.get("create_intent_hash"), str) \
                    and hmac.compare_digest(existing["create_intent_hash"], intent_hash)
                token_matches = isinstance(existing.get("token_hash"), str) \
                    and hmac.compare_digest(existing["token_hash"], token_hash)
                if not (contract_matches and identity_matches and intent_matches and token_matches):
                    raise ReviewError(
                        "this review create identity is already bound to another request",
                        kind="conflict", link_id=link_id)
                if not self._valid_recovery_record(
                        existing, link_id=link_id, run_id=canonical_run,
                        generation=expected, ttl=ttl,
                        include_evidence=include_evidence):
                    raise ReviewError(
                        "stored review-link metadata is invalid", kind="storage")
                return token, _public(existing), True
            if state == "abandoned_empty":
                try:
                    path.unlink()
                except OSError as exc:
                    raise ReviewError(
                        "review-link storage is unavailable", kind="storage") from exc
                state = "absent"
            if state != "absent":
                raise ReviewError("review-link storage is unavailable", kind="storage")

            current = exact_review_generation(generation)
            if current is None or not hmac.compare_digest(current, expected):
                raise ReviewError(
                    "the run generation changed before review-link creation",
                    kind="generation_conflict",
                    expected_generation=expected,
                    current_generation=current)
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                reserved_path, reserved = self._reserve_exact(link_id)
            except OSError as exc:
                raise ReviewError(
                    "review-link storage is unavailable", kind="storage") from exc
            if not reserved:
                # The global lock makes this possible only through an uncoordinated legacy writer or
                # external filesystem mutation. Never infer that the new occupant is our request.
                raise ReviewError("review-link storage is unavailable", kind="storage")
            record = self._record(
                canonical_run, current, ttl, include_evidence, link_id, token,
                create_contract=_CREATE_CONTRACT,
                create_identity_hash=identity_hash,
                create_intent_hash=intent_hash)
            self._publish_reserved(reserved_path, record)
            return token, _public(record), False

    def status(self, record: dict, *, current_generation: object = _CURRENT_GENERATION_UNSET,
               now: float | None = None) -> str:
        """Return the owner-facing lifecycle with the established fail-closed precedence."""
        if record.get("revoked_at") is not None:
            return "revoked"
        moment = time.time() if now is None else now
        if self._number(record.get("expires_at")) <= moment:
            return "expired"
        if current_generation is not _CURRENT_GENERATION_UNSET:
            current = exact_review_generation(current_generation)
            if current is None or record.get("generation") != current:
                return "stale"
        return "active"

    def list_for_run(self, run_id: str) -> list[dict]:
        out = []
        records = (self._read(path) for path in self.directory.glob("rvl_*.json")) \
            if self.directory.exists() else ()
        for record in records:
            if record is None:
                continue
            if record.get("run_id") != str(run_id):
                continue
            item = _public(record)
            item["status"] = self.status(record)
            out.append(item)
        return sorted(out, key=lambda r: self._number(r.get("created_at")), reverse=True)

    def revoke(self, run_id: str, link_id: str) -> dict:
        with self._store_lock():
            path = self._path(link_id)
            state, record = self._strict_existing(path)
            if state != "valid" or record is None or record.get("run_id") != str(run_id):
                if state in {"unreadable", "corrupt", "fresh_empty", "abandoned_empty"}:
                    raise ReviewError("review-link storage is unavailable", kind="storage")
                raise ReviewError("no such review link", kind="not_found")
            if record.get("revoked_at") is None:
                record["revoked_at"] = time.time()
                try:
                    self._save(path, record)
                except OSError as exc:
                    raise ReviewError(
                        "review-link storage is unavailable", kind="storage") from exc
            return _public(record)

    def resolve(self, token: str, *, now: float | None = None) -> dict:
        """Resolve a bearer token and return safe metadata, or raise a typed error."""
        token = str(token or "")
        parts = token.split("_", 2)
        if (len(parts) != 3 or parts[0] != "rv" or len(parts[1]) not in {12, 32}
                or len(parts[2]) != 43
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in parts[2])):
            raise ReviewError("invalid review link")
        link_id = "rvl_" + parts[1]
        record = self._read(self._path(link_id))
        if record is None or not hmac.compare_digest(str(record.get("token_hash") or ""), _digest(token)):
            raise ReviewError("invalid review link")
        scopes = record.get("scopes")
        if (record.get("id") != link_id or not isinstance(record.get("run_id"), str)
                or not record["run_id"] or not isinstance(scopes, list)
                or not all(isinstance(scope, str) for scope in scopes)
                or "summary" not in scopes or not set(scopes).issubset({"summary", "evidence"})
                or _finite_number(record.get("created_at")) is None):
            raise ReviewError("invalid review link")
        if exact_review_generation(record.get("generation")) is None:
            # Pre-generation capabilities and hand-edited malformed records cannot be safely
            # retargeted to whichever run now occupies the same id.
            raise ReviewError(
                "this review link has no valid run generation binding", kind="generation")
        revoked_at = record.get("revoked_at")
        if revoked_at is not None:
            # A malformed marker still fails closed as revoked, while _public normalizes it so the
            # owner link list cannot be crashed by NaN/Infinity in a hand-edited record.
            raise ReviewError("this review link was revoked", kind="revoked")
        if self._number(record.get("expires_at")) <= (time.time() if now is None else now):
            raise ReviewError("this review link expired", kind="expired")
        return _public(record)


def review_request_allowed(record: dict, method: str, path: str) -> bool:
    """Review principals may call only the dedicated read namespace.

    The namespace derives its run from ``record`` and never accepts a client-supplied run id.  Scope
    checks happen again inside each handler.  Any owner route presented with a review credential is
    therefore denied even if the browser accidentally renders an owner-only control.
    """
    if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        return False
    return path == "/api/review" or path.startswith("/api/review/")
