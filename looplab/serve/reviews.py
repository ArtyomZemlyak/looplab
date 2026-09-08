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

from contextlib import contextmanager
import hmac
import json
import math
import secrets
import threading
import time
from pathlib import Path

from looplab.core.atomicio import atomic_write_text
from looplab.core.jsonutil import valid_digest_ref
from looplab.serve.capability_store import (
    capability_store_lock,
    exact_request_id,
    exact_token_secret,
    publish_reserved,
    recovery_bearer,
    recovery_digest,
    reservation_state,
    reserve_exact_id,
    reserve_unique_id,
    store_process_lock,
    token_digest as _digest,
)


DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
MIN_TTL_SECONDS = 5 * 60
MAX_TTL_SECONDS = 30 * 24 * 60 * 60
REVIEW_HEADER = "X-LoopLab-Review"
_CREATE_CONTRACT = 1
_CURRENT_GENERATION_UNSET = object()


class ReviewError(ValueError):
    """Invalid review-link operation or credential."""

    def __init__(self, message: str, *, kind: str = "invalid", **metadata):
        super().__init__(message)
        self.kind = kind
        self.metadata = metadata


# The create-recovery FIELD validators and the derivation below are `capability_store`'s since
# 2026-09-08: `ShareStore` needed the SAME protocol, and a second copy of these bytes would hand a
# retrying client a token that authenticates nothing (doc 25 SC-10).  The review spellings stay
# exported here because the review router imports them by these names.
exact_review_request_id = exact_request_id
exact_review_token_secret = exact_token_secret


def _recovery_identity(run_id: str, request_id: str) -> tuple[str, str]:
    identity_hash = recovery_digest("looplab-review-create-id-v1", {
        "request_id": request_id,
        "run_id": run_id,
        "v": _CREATE_CONTRACT,
    })
    return "rvl_" + identity_hash[:32], identity_hash


def _recovery_token(link_id: str, token_secret: bytes) -> str:
    suffix = link_id[4:]
    return f"rv_{suffix}_{recovery_bearer('looplab-review-bearer-v1', token_secret, link_id)}"


def _recovery_intent(run_id: str, generation: str, ttl_seconds: int,
                     include_evidence: bool) -> str:
    return recovery_digest("looplab-review-create-intent-v1", {
        "expected_generation": generation,
        "include_evidence": include_evidence,
        "run_id": run_id,
        "ttl_seconds": ttl_seconds,
        "v": _CREATE_CONTRACT,
    })


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
    return value if valid_digest_ref(value) else None


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
        return store_process_lock(self.directory)

    @contextmanager
    def _store_lock(self):
        """Require one lock for deterministic ids across every run and server worker.

        The run sequencer cannot protect the global ``.reviews`` namespace: two different runs hold
        different command locks while their truncated on-disk ids could still collide.  Pair a
        process-wide lock with a required OS lock, and fail closed when the filesystem cannot provide
        that ordering.  There is intentionally no thread-only fallback.

        The protocol itself is `capability_store.capability_store_lock`, shared with `ShareStore`
        (doc 25 SC-10). Only the two SENTENCES are this store's own: a timeout and a lock failure
        are reported differently here and identically there, which is why the core takes both.
        """
        with capability_store_lock(
                self._lock, self._lock_path,
                on_timeout=lambda: ReviewError(
                    "timed out waiting for the review-link store lock", kind="storage"),
                on_unavailable=lambda: ReviewError(
                    "review-link storage is temporarily unavailable", kind="storage")):
            yield

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

    # The reservation protocol itself lives in `capability_store` and is shared with `ShareStore`
    # (doc 25 SC-10). These stay as methods because they are read as part of this store's own
    # lifecycle at four call sites, and because a subclass/monkeypatch seam on them predates the
    # extraction; each one is now a binding of the shared rule to this store's error type.
    _strict_existing = staticmethod(reservation_state)

    def _publish_reserved(self, path: Path, record: dict) -> None:
        """Publish one owned reservation, preserving any non-empty uncertain result."""
        publish_reserved(
            path, record, save=self._save,
            on_unavailable=lambda: ReviewError(
                "review-link storage is unavailable", kind="storage"))

    def _reserve(self) -> tuple[str, Path]:
        """Atomically reserve a fresh id without ever overwriting an existing capability."""
        self.directory.mkdir(parents=True, exist_ok=True)
        return reserve_unique_id(
            mint=lambda: "rvl_" + secrets.token_hex(16), path_for=self._path, attempts=128,
            on_exhausted=lambda: ReviewError("could not allocate a unique review link"))

    def _reserve_exact(self, link_id: str) -> tuple[Path, bool]:
        path = self._path(link_id)
        return path, reserve_exact_id(path)

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
