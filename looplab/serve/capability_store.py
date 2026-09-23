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
* `polled_store_lock` — the same "process lock THEN OS lock" order for the two assistant stores
  that are NOT capability stores (`WatchStore`, `SessionStore`). It waits a sibling out instead of
  refusing, and degrades on a locking CAPABILITY gap instead of failing closed; its docstring says
  why each difference is the right one for those stores and the wrong one for this module's.
  `hold_lease` / `lease_is_held` are the pair `WatchStore`'s owner lease is built from: a lock a
  live process keeps for its lifetime, and the probe that asks whether anyone still keeps it. They
  live here, beside the other OS-lock spellings, so `assistant_watch.py` reaches the lock without
  an import edge into the event store it must never append to.
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
* The CREATE-RECOVERY derivation — `exact_request_id`, `exact_token_secret`, `canonical_bytes`,
  `recovery_digest`, `recovery_bearer`. A lost response to a create leaves the client holding no
  token while a live capability exists, and a plain retry then mints a SECOND one: an un-revoked
  bearer nobody holds. The fix is that the CLIENT, not the server, owns the identity — a random
  request id and a 256-bit secret it keeps — so the id is derived from (subject, request id) and
  the bearer is an HMAC of that secret over the id. A retry lands on the same record and
  reconstructs the same token, which the store still never persists. Both sides of that derivation
  are pure functions of the client's envelope, and a server that computed one byte differently from
  its sibling would hand back a token that authenticates nothing: they could only ever agree by
  hand, which is precisely what belongs here.

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

import base64
from contextlib import ExitStack, contextmanager
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
import uuid
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


# --- the create-recovery derivation, shared by both stores (doc 25 SC-10) -------------------------

_TOKEN_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def canonical_bytes(value: dict) -> bytes:
    """The one serialization a hash is taken over. Sorted, compact and ASCII-escaped, so the digest
    depends on the FACTS in the envelope and never on a dict order or a JSON writer's spacing."""
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def exact_request_id(value: object) -> str | None:
    """Return only a canonical lowercase RFC 4122 UUIDv4 create identity."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if (parsed.variant == uuid.RFC_4122 and parsed.version == 4
                     and str(parsed) == value) else None


def exact_token_secret(value: object) -> bytes | None:
    """Decode only a canonical unpadded base64url 256-bit create-recovery secret.

    Canonical, not merely decodable: two spellings of one secret would derive two bearers for one
    record, so the round trip has to come back byte-identical before the value is accepted.
    """
    if not isinstance(value, str) or _TOKEN_SECRET.fullmatch(value) is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, TypeError):
        return None
    if len(decoded) != 32:
        return None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    return decoded if hmac.compare_digest(canonical, value) else None


def recovery_digest(label: str, payload: dict) -> str:
    """A domain-separated SHA-256 over one create-recovery envelope.

    `label` is what keeps an identity hash, an intent hash and a sibling store's hashes from ever
    being the same number over the same facts, so a digest lifted from one can never be replayed as
    another. The NUL is the separator: a label is ASCII and a canonical JSON body starts with `{`,
    so no label/body pair can be read two ways.
    """
    return hashlib.sha256(
        label.encode("ascii") + b"\0" + canonical_bytes(payload)).hexdigest()


def recovery_bearer(label: str, secret: bytes, link_id: str) -> str:
    """The bearer secret a client can reconstruct: HMAC(client secret, label \0 link id), base64url.

    The store persists only `token_digest` of the assembled token, exactly as it does for a random
    one. What changes is who can derive it a second time: the holder of the 256-bit secret, and
    nobody else — the record itself carries nothing that would let a reader recompute this.
    """
    material = hmac.new(
        secret, label.encode("ascii") + b"\0" + link_id.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")


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


# How long a `polled_store_lock` caller waits for a SIBLING PROCESS inside the same store section.
# Those sections are one small read plus one atomic write (a transcript append with its fsync at
# worst), so anything near this bound is a wedged or stopped holder, not a slow one.
STORE_MUTATION_TIMEOUT_SECONDS = 10.0
_STORE_MUTATION_POLL_SECONDS = 0.005
# Lock paths the CURRENT thread holds through `polled_store_lock`. The interprocess half is a
# `flock` on a fresh descriptor, which conflicts with the same thread's own earlier descriptor —
# so a nested acquisition would not re-enter, it would poll against itself until the timeout and
# then report cross-process contention that never happened. Refused instead, exactly as
# `run_commands.py::RunCommandService.sequence` refuses its own re-entry.
_POLLED_HELD = threading.local()


@contextmanager
def polled_store_lock(process_lock, lock_path: Path, *,
                      timeout: float = STORE_MUTATION_TIMEOUT_SECONDS,
                      prepare: Optional[Callable[[], None]] = None):
    """Process lock THEN a POLLED cross-process lock on `lock_path` (review 2026-09-22, SRV1-03).

    `WatchStore` and `SessionStore` serialized their read-modify-writes with a `threading.Lock`
    alone, which orders the threads of ONE server. A second server over the same run root is a
    first-class path — `looplab tui` starts its own through `ensure_server`, and a hub restart can
    overlap the old process — and two processes interleaving `WatchStore.claim` both moved one due
    watch `armed -> waking`: two paid wake-up turns for one wake-up. Two `append_if_len`s could both
    pass the same length check the same way. This is the missing cross-process half, spelled beside
    `capability_store_lock` so the two readings of "process lock, then OS lock" sit side by side.
    It differs from that one on purpose, three times:

    * contention is WAITED OUT (a bounded poll) rather than refused at once. The sections it guards
      are one small read and one atomic write, and a scheduler settle or a transcript append that
      failed on a sibling's millisecond-long section would strand a claim or drop a paid reply;
    * a locking CAPABILITY gap (a mount whose advisory locks are unsupported) degrades to the
      process lock — the whole historical guarantee of these stores — instead of refusing. A
      capability store guards authorization state and must fail closed; a watch or transcript store
      that refused would take the assistant down on exactly the network mounts it runs from;
    * an inaccessible lock PATH still raises (`interprocess_lock`'s never-degrade half), and running
      out of `timeout` raises `TimeoutError` — an `OSError`, the class every caller of these stores
      already treats as the store's own storage failure.

    `prepare` runs after the process lock and before the OS lock (a store that must create its
    directory first). Not re-entrant on one thread for one path — see `_POLLED_HELD`.
    """
    from looplab.events.eventstore import InterprocessLockContended, interprocess_lock

    key = os.path.normcase(os.path.abspath(os.fspath(lock_path)))
    held = getattr(_POLLED_HELD, "paths", None)
    if held is None:
        held = _POLLED_HELD.paths = set()
    if key in held:
        raise RuntimeError(
            "polled_store_lock re-entered on one thread: its interprocess half is not re-entrant, "
            "so the nested acquisition would wait on this thread's own descriptor. Hoist the inner "
            "call out of the outer locked section.")
    deadline = time.monotonic() + max(0.0, float(timeout))
    if not process_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("timed out waiting for the in-process store lock")
    try:
        if prepare is not None:
            prepare()
        owner = ExitStack()
        while True:
            try:
                # `required=False`: the capability gap degrades (second bullet above); an
                # inaccessible path raises either way, and contention is the one retryable answer.
                owner.enter_context(interprocess_lock(lock_path, blocking=False))
                break
            except InterprocessLockContended:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "timed out waiting for another process's store section") from None
                time.sleep(_STORE_MUTATION_POLL_SECONDS)
        held.add(key)
        try:
            with owner:
                yield
        finally:
            held.discard(key)
    finally:
        process_lock.release()


def hold_lease(lock_path: Path) -> Optional[ExitStack]:
    """Take a NON-blocking, REQUIRED lock on `lock_path` and KEEP it: the returned stack holds it
    until closed — or until the process dies, when the kernel drops it, which is the whole point of
    a lease. None when it cannot be held: contended, a mount without advisory locks, or a path that
    will not open. A caller must treat None as "no lease", never as an error to surface."""
    from looplab.events.eventstore import (
        EventStoreLockError, InterprocessLockContended, interprocess_lock)

    lease = ExitStack()
    try:
        lease.enter_context(interprocess_lock(lock_path, required=True, blocking=False))
    except (EventStoreLockError, InterprocessLockContended, OSError):
        lease.close()
        return None
    return lease


def lease_is_held(lock_path: Path) -> bool:
    """Does ANOTHER holder keep `lock_path` locked right now?

    A probe that takes the lock for an instant and gives it back. True ONLY on contention: an absent
    file, an unopenable one and a mount whose advisory locks are unsupported all answer False,
    because none of them PROVES a holder — which is exactly what a caller about to settle on "the
    holder is gone" needs to be told. (On a lock-less mount that is the historical behaviour: no
    lease could ever have been held there.)"""
    from looplab.events.eventstore import InterprocessLockContended, interprocess_lock

    if not lock_path.exists():
        return False
    try:
        with interprocess_lock(lock_path, blocking=False):
            return False
    except InterprocessLockContended:
        return True
    except OSError:
        return False


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
