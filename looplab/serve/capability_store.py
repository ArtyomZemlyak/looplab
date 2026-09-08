"""The bearer-capability store CORE — one spelling of the rules `ReviewStore` and `ShareStore` share.

Both stores are one-file-per-capability directories of bearer credentials: a review link
(`serve/reviews.py`) names one run for a read-only reviewer, a share link (`serve/assistant.py`)
names one assistant session for a reader who has the URL. Neither ever persists the bearer value —
only a SHA-256 digest — and both mint an id, reserve its path, and publish one atomic JSON file.

Doc 25 SC-10 measured that as one concept with two implementations and materially different
guarantees; the 2026-08-04 pass closed the locking half by writing `ReviewStore`'s contract out a
second time in `assistant.py`, and left the note that the two were still separate implementations.
This module is that extraction. What lives here is the part that is genuinely ONE PROTOCOL and
whose copies could only ever agree by hand:

* `store_process_lock` — the per-path in-process lock TABLE. Two store objects over one directory
  must contend on one lock, so the table is keyed on the normcased absolute path and not on the
  instance. `abspath` is lexical on purpose: a missing store directory must not turn constructing a
  store into an I/O failure at startup.
* `capability_store_lock` — process lock (bounded timeout) THEN a required, non-blocking
  interprocess lock, failing closed with the caller's own error type. There is deliberately no
  thread-only fallback: a filesystem that cannot provide the ordering must refuse, because what the
  ordering protects is a read-modify-write over live capabilities (two workers interleaving a
  revocation leaves alive a link whose owner was told it was dead).
* `reserve_unique_id` / `reserve_exact_id` — `O_EXCL` reservation. Randomness makes a collision
  extraordinarily unlikely, but relying on probability alone would let a collision REPLACE an
  existing token digest, and `O_EXCL` is also what makes the reservation safe against a writer that
  does not hold the store lock at all (a rolling upgrade, or an uncoordinated legacy worker).
* `reservation_state` — the three-way classification of what sits at a reserved path: absent, an
  in-flight or abandoned EMPTY reservation, valid JSON, or unreadable/corrupt. An empty file is the
  fail-closed footprint of a process that died between reserving and publishing; it is never
  authorization, and it is never silently treated as free space either.
* `publish_reserved` — publish one OWNED reservation, preserving any non-empty uncertain result and
  healing only the empty footprint this caller created.
* `token_digest` — the one hashing of a bearer value.

**What deliberately stays local to each store, because sharing it would LOOSEN it:**

* `resolve`. `ShareStore.resolve` returns ONE indistinguishable `None` for every failure, because a
  reader who can tell a revoked link from a never-existing one has a session-existence oracle.
  `ReviewStore.resolve` raises TYPED errors naming revoked / expired / generation, because its
  reader is the owner's own guest and the surface must say why the link stopped working. A merged
  resolve would have to pick one, and either choice is a regression for the other store.
* TTL validation. The two are not one rule spelled twice: `ShareStore` refuses a non-integer float
  and a `bool`, while `ReviewStore`'s ordinary path truncates a float and only its recovery path
  demands an exact `int`. Parameterizing every one of those differences produces a validator whose
  configuration IS the duplicated code, and the bounds and messages are per-surface anyway.
* The record schemas and their validators (`_validated_record`, `_valid_recovery_record`). These
  are the fail-closed readings of durable authorization state, and they describe different records
  (a session + transcript bound vs a run + generation + scopes).
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# One table for BOTH stores. They key on different directories, so sharing the table costs nothing
# and removes the second place a "keyed on the instance" regression could be reintroduced.
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}

STORE_LOCK_TIMEOUT_SECONDS = 5.0

# How long an EMPTY reservation is treated as possibly in-flight rather than abandoned. A rolling
# upgrade may overlap an older worker which reserves the final path before its atomic replace and
# does not know about the store lock; that window is milliseconds, and one second of grace is
# generous for it while still bounding how long a crashed worker's footprint blocks its own id.
FRESH_RESERVATION_SECONDS = 1.0
_EMPTY_RESERVATION_POLLS = 21


def token_digest(token: str) -> str:
    """The stored form of a bearer value. The token itself is never persisted by either store."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def store_process_lock(directory) -> threading.Lock:
    """The in-process lock for one store PATH, shared by every store object over that path."""
    # abspath is lexical and cannot turn a transient/missing store directory into a startup I/O
    # failure; endpoint-time filesystem failures are translated to the structured storage error.
    key = os.path.normcase(os.path.abspath(os.fspath(directory)))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def capability_store_lock(process_lock: threading.Lock, lock_path: Path, *,
                          on_timeout: Callable[[], BaseException],
                          on_unavailable: Callable[[], BaseException],
                          prepare: Optional[Callable[[], None]] = None,
                          timeout: float = STORE_LOCK_TIMEOUT_SECONDS):
    """Require one lock for deterministic ids across every caller and server worker.

    A run sequencer cannot protect a global capability namespace: two different runs hold different
    command locks while their on-disk ids live in one directory. Pair a process-wide lock with a
    required OS lock, and fail closed when the filesystem cannot provide that ordering. There is
    intentionally no thread-only fallback.

    Non-blocking acquisition gives the HTTP layer a bounded, safely retryable 503 instead of parking
    a request thread behind another worker indefinitely. `prepare` runs INSIDE the storage-failure
    boundary so a store that must create its directory first reports that failure the same way.

    The caller's body runs inside the failure boundary too, exactly as both hand-written copies did:
    a raw `OSError` escaping a locked mutation is a storage failure of this store, and it must not
    reach an HTTP handler as a path-bearing 500.
    """
    from looplab.events.eventstore import (
        EventStoreLockError, InterprocessLockContended, interprocess_lock)

    if not process_lock.acquire(timeout=timeout):
        raise on_timeout()
    try:
        try:
            if prepare is not None:
                prepare()
            with interprocess_lock(lock_path, required=True, blocking=False):
                yield
        except (EventStoreLockError, InterprocessLockContended, OSError) as exc:
            raise on_unavailable() from exc
    finally:
        process_lock.release()


def reservation_state(path: Path, *, wait: bool = True) -> tuple[str, Optional[dict]]:
    """Distinguish absence, an abandoned reservation, and corrupt persisted state.

    Returns one of `absent` / `unreadable` / `corrupt` / `valid` / `fresh_empty` /
    `abandoned_empty`, with the parsed record only for `valid`. Non-empty malformed state is NEVER
    treated as free space.

    `wait=True` gives the narrow empty-file window a bounded chance to finish, which is what a
    caller about to CLAIM this exact id needs. A sweep over a whole directory passes `wait=False`:
    it is deciding whether to delete someone else's footprint, and sleeping per entry would turn a
    prune into seconds of latency for a state that its own age already settles.
    """
    info = None
    for attempt in range(_EMPTY_RESERVATION_POLLS if wait else 1):
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
        if wait and attempt < _EMPTY_RESERVATION_POLLS - 1:
            time.sleep(0.01)
    assert info is not None
    age = max(0.0, time.time() - info.st_mtime)
    return ("abandoned_empty" if age >= FRESH_RESERVATION_SECONDS else "fresh_empty"), None


def remove_failed_reservation(path: Path) -> None:
    """Drop a reservation this caller created and could not publish. Best effort by design: the
    reservation is already fail-closed (an empty file authorizes nothing), so a failure to unlink it
    must not mask the storage error that is actually being reported."""
    try:
        path.unlink()
    except OSError:
        pass


def publish_reserved(path: Path, record: dict, *, save: Callable[[Path, dict], None],
                     on_unavailable: Callable[[], BaseException]) -> None:
    """Publish one owned reservation, preserving any non-empty uncertain result."""
    try:
        save(path, record)
        return
    except Exception as exc:  # noqa: BLE001 - normalize storage failures without leaking paths
        state, existing = reservation_state(path)
        if state == "valid" and existing == record:
            # os.replace completed before a later filesystem operation reported failure.
            return
        if state in {"fresh_empty", "abandoned_empty"}:
            # This caller created the reservation and still holds the global transaction lock.
            remove_failed_reservation(path)
        raise on_unavailable() from exc


def reserve_exact_id(path: Path) -> bool:
    """Reserve one NAMED path, or report that something already occupies it.

    `O_EXCL | O_CREAT` fails on an existing file AND on a symlink (including a dangling one), so
    this is the existence check and the claim in one uninterruptible step.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def reserve_unique_id(*, mint: Callable[[], str], path_for: Callable[[str], Optional[Path]],
                      attempts: int, on_exhausted: Callable[[], BaseException],
                      verify: Optional[Callable[[Path], bool]] = None) -> tuple[str, Path]:
    """Atomically reserve a fresh id without ever overwriting an existing capability.

    Randomness makes a collision extraordinarily unlikely, but relying on probability alone would
    let a collision replace an existing token digest. ``O_EXCL`` also makes this safe across threads
    and multiple server workers. The empty reservation is fail-closed if the process crashes before
    the atomic JSON replacement.

    `verify(path)` is the store's own pathname boundary, re-checked for each candidate BEFORE the
    claim (a parent redirected between the directory check and this call must not be written into).
    """
    for _ in range(attempts):
        link_id = mint()
        path = path_for(link_id)
        if path is None:
            continue
        if verify is not None and not verify(path):
            continue
        if reserve_exact_id(path):
            return link_id, path
    raise on_exhausted()
