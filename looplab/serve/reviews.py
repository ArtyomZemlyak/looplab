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

import hashlib
import hmac
import json
import math
import os
import secrets
import time
from pathlib import Path

from looplab.core.atomicio import atomic_write_text


DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
MIN_TTL_SECONDS = 5 * 60
MAX_TTL_SECONDS = 30 * 24 * 60 * 60
REVIEW_HEADER = "X-LoopLab-Review"
_GENERATION_HEX = frozenset("0123456789abcdef")


class ReviewError(ValueError):
    """Invalid review-link operation or credential."""

    def __init__(self, message: str, *, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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

    def create(self, run_id: str, *, generation: str,
               ttl_seconds: int = DEFAULT_TTL_SECONDS,
               include_evidence: bool = False) -> tuple[str, dict]:
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ReviewError("expiry must be a whole number of seconds") from exc
        if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
            raise ReviewError(
                f"expiry must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds")
        exact_generation = exact_review_generation(generation)
        if exact_generation is None:
            raise ReviewError(
                "run generation is missing or malformed", kind="generation")
        now = time.time()
        link_id, path = self._reserve()
        token = f"rv_{link_id[4:]}_{secrets.token_urlsafe(32)}"
        record = {
            "id": link_id,
            "token_hash": _digest(token),
            "run_id": str(run_id),
            "generation": exact_generation,
            "scopes": ["summary"] + (["evidence"] if include_evidence else []),
            "created_at": now,
            "expires_at": now + ttl,
            "revoked_at": None,
        }
        try:
            self._save(path, record)
        except BaseException:
            # Do not leave a permanent empty reservation after an ordinary write failure.  A hard
            # process crash can still leave one; readers skip it and its random id is never reused.
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return token, _public(record)

    def list_for_run(self, run_id: str) -> list[dict]:
        now = time.time()
        out = []
        records = (self._read(path) for path in self.directory.glob("rvl_*.json")) \
            if self.directory.exists() else ()
        for record in records:
            if record is None:
                continue
            if record.get("run_id") != str(run_id):
                continue
            item = _public(record)
            item["status"] = ("revoked" if record.get("revoked_at") is not None else
                              "expired" if self._number(record.get("expires_at")) <= now else "active")
            out.append(item)
        return sorted(out, key=lambda r: self._number(r.get("created_at")), reverse=True)

    def revoke(self, run_id: str, link_id: str) -> dict:
        path = self._path(link_id)
        record = self._read(path)
        if record is None or record.get("run_id") != str(run_id):
            raise ReviewError("no such review link", kind="not_found")
        if record.get("revoked_at") is None:
            record["revoked_at"] = time.time()
            self._save(path, record)
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
